import os
import json

import streamlit as st
from dotenv import load_dotenv

from google import genai
from google.genai import types
from brd_models import BRDData, Requirement, Stakeholder, ProjectOverview, Assumption

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


def validate_and_create_brd_data(data: dict, original_notes: str) -> BRDData:
    """
    Validates the extracted data from Gemini against the original notes
    and maps it to BRDData objects.
    """
    all_assumptions = [Assumption(**a) for a in data.get("assumptions", [])]

    validated_frs, fr_assumptions = _validate_requirements(data.get("functional_requirements", []), original_notes)
    validated_nfrs, nfr_assumptions = _validate_requirements(data.get("non_functional_requirements", []), original_notes)
    all_assumptions.extend(fr_assumptions)
    all_assumptions.extend(nfr_assumptions)

    brd_data = BRDData(
        project_title=data.get("project_title", "Untitled Project"),
        project_overview=ProjectOverview(**data.get("project_overview", {})),
        business_objectives=data.get("business_objectives", []),
        stakeholders=[Stakeholder(**s) for s in data.get("stakeholders", [])],
        functional_requirements=validated_frs,
        non_functional_requirements=validated_nfrs,
        assumptions=all_assumptions,
        open_questions=data.get("open_questions", []),
        deferred_or_out_of_scope_items=data.get("deferred_or_out_of_scope_items", []),
        background_information=data.get("background_information", []),
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
    1.  **Confirmed Requirements**: Only classify an item as a 'functional_requirement' or 'non_functional_requirement' if it is explicitly stated as a requirement. Each requirement MUST include a 'source_evidence' field containing the exact quote from the notes that supports it. If no direct evidence exists, do not create a requirement.
    1.  **Confirmed Requirements**: Only classify an item as a 'functional_requirement' or 'non_functional_requirement' if it is explicitly stated as a requirement. Each requirement MUST include a 'source_evidence' field containing a VERBATIM, COPY-PASTED SUBSTRING from the original notes that directly supports it. Do NOT paraphrase, summarize, or alter this evidence in any way. If no direct, verbatim evidence exists, do not create a requirement.
    2.  **Evidence Integrity**: The `source_evidence` must be a single, contiguous block of text copied exactly from the notes, including any original line breaks or indentation.
    3.  **Open Questions**: Capture any points that are unclear, unresolved, or need follow-up as 'open_questions'.
    4.  **Out of Scope**: Capture anything explicitly deferred, parked, or excluded as 'deferred_or_out_of_scope_items'.
    5.  **Stakeholders**: List all mentioned individuals. For the `requirement` field in the JSON, only include direct asks or requests. For suggestions, recommendations, or technical flags (e.g., "probably worth a spike"), place them in the `concern` field instead.
    6.  **Source of Truth**: Do not "clean up" or "fix" the notes. If the notes are ambiguous, reflect that ambiguity in your output, likely as an open question or assumption.

    Return the output as a single JSON object. Do not include any text or formatting before or after the JSON object. The JSON schema should be:

    {{
      "project_title": "Project Title from Notes (or 'Untitled Project')",
      "project_overview": {{
        "project_name": "Project Name if available",
        "date": "Date of meeting if available"
      }},
      "business_objectives": ["List of strings"],
      "stakeholders": [
        {{"name": "string", "role": "string", "concern": "string", "requirement": "string"}}
      ],
      "functional_requirements": [
        {{"requirement_id": "FR-1", "statement": "The system shall...", "source_evidence": "Exact quote from notes"}}
      ],
      "non_functional_requirements": [
        {{"requirement_id": "NFR-1", "statement": "The system must...", "source_evidence": "Exact quote from notes"}}
      ],
      "assumptions": [
        {{"statement": "What is being assumed", "confirmation_needed": "Who to confirm with or what to clarify"}}
      ],
      "open_questions": ["List of strings"],
      "deferred_or_out_of_scope_items": ["List of strings"],
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
    lines.append("## Project Overview")
    lines.append(f"**Project Name:** {brd_data.project_overview.project_name or 'Not specified'}")
    lines.append(f"**Date:** {brd_data.project_overview.date or 'Not specified'}")

    # Business Objectives
    lines.append("\n## Business Objectives")
    if brd_data.business_objectives:
        for obj in brd_data.business_objectives:
            lines.append(f"- {obj}")
    else:
        lines.append("None specified.")

    # Stakeholders
    lines.append("\n## Stakeholders")
    if brd_data.stakeholders:
        for s in brd_data.stakeholders:
            role = f" — {s.role}" if s.role else ""
            lines.append(f"### {s.name}{role}")
            if s.concern:
                lines.append(f"- **Concern:** {s.concern}")
            if s.requirement:
                lines.append(f"- **Request:** {s.requirement}")
    else:
        lines.append("None specified.")

    # Scope
    lines.append("\n## Scope")
    lines.append("### In Scope")
    lines.append("#### Functional Requirements")
    if brd_data.functional_requirements:
        for req in brd_data.functional_requirements:
            lines.append(f"**{req.requirement_id}:** {req.statement}")
            lines.append(f"> _Source Evidence: {req.source_evidence}_")
    else:
        lines.append("None specified.")
    
    lines.append("\n#### Non-Functional Requirements")
    if brd_data.non_functional_requirements:
        for req in brd_data.non_functional_requirements:
            lines.append(f"**{req.requirement_id}:** {req.statement}")
            lines.append(f"> _Source Evidence: {req.source_evidence}_")
    else:
        lines.append("None specified.")

    lines.append("\n### Deferred or Out of Scope")
    if brd_data.deferred_or_out_of_scope_items:
        for item in brd_data.deferred_or_out_of_scope_items:
            lines.append(f"- {item}")
    else:
        lines.append("None specified.")

    # Assumptions
    lines.append("\n## Assumptions")
    for assumption in brd_data.assumptions:
        lines.append(f"- **Assumption:** {assumption.statement}")
        lines.append(f"  - **Confirmation Needed:** {assumption.confirmation_needed}")

    # Open Questions
    lines.append("\n## Open Questions")
    for q in brd_data.open_questions:
        lines.append(f"- {q}")

    return "\n".join(lines)


def display_brd(brd_data: BRDData):
    """Renders the entire BRD in the Streamlit UI."""
    st.header(brd_data.project_title)

    st.subheader("Project Overview")
    st.write(f"**Project Name:** {brd_data.project_overview.project_name or 'Not specified in source notes'}")
    st.write(f"**Date:** {brd_data.project_overview.date or 'Not specified in source notes'}")

    st.subheader("Business Objectives")
    for objective in brd_data.business_objectives:
        st.write(f"- {objective}")

    st.subheader("Stakeholders")
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

    st.header("Scope")
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

    st.subheader("Assumptions")
    for assumption in brd_data.assumptions:
        st.warning(f"**Assumption:** {assumption.statement}\n\n**Confirmation Needed:** {assumption.confirmation_needed}")

    st.subheader("Open Questions")
    for question in brd_data.open_questions:
        st.warning(question)

    st.subheader("Background Information / Technical Context")
    for item in brd_data.background_information:
        st.write(f"- {item}")


# --- Main Streamlit App ---

st.title("Auto-BRD Generator")
st.write(
    "Convert raw meeting notes into a structured Business Requirements Document."
)

meeting_notes = st.text_area(
    "Paste meeting notes",
    height=300,
    placeholder="Paste your meeting notes here...",
)

if st.button("Generate BRD"):
    if not meeting_notes.strip():
        st.error("Please paste your meeting notes before generating the BRD.")
    else:
        if not GEMINI_API_KEY:
            st.error("Gemini API key is not configured. Please set it in your .env file.")
        elif not CLIENT:
            st.error("Gemini client could not be initialized. Please check your API key.")
        else:
            try:
                with st.spinner("Generating BRD from your notes..."):
                    brd_data = generate_brd_from_notes(meeting_notes)

                # --- Display the generated BRD ---
                display_brd(brd_data)

                # --- Add Download Button ---
                st.divider()
                markdown_content = convert_brd_to_markdown(brd_data)
                st.download_button(
                    label="Download BRD as Markdown",
                    data=markdown_content,
                    file_name=f"{brd_data.project_title.replace(' ', '_')}_BRD.md",
                    mime="text/markdown",
                )
            except json.JSONDecodeError:
                st.error("The response from the AI was not valid JSON. Please try again.")
            except Exception as e:
                st.error(f"An unexpected error occurred: {e}")
