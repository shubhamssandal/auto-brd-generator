# Auto-BRD Generator from Meeting Notes

This project is a Streamlit web application that converts raw, unstructured meeting notes into a draft Business Requirements Document (BRD). It is designed as a portfolio piece to demonstrate skills in business analysis, requirements engineering, and the responsible application of Large Language Models (LLMs).

## The Problem It Solves

Business Analysts and Product Managers often spend significant time manually sifting through messy meeting notes to extract and structure key decisions, requirements, and action items. This manual process is time-consuming and prone to error. This tool aims to automate the initial draft of a BRD, freeing up the analyst to focus on higher-value tasks like validation, clarification, and strategic planning.

## Core Principle: The Notes Are the Source of Truth

The application's most important feature is its commitment to traceability. It will **never** present an AI-inferred statement as a confirmed fact.

- **Confirmed Requirements** must have direct, verifiable `Source Evidence` from the original notes.
- **Assumptions** are clearly flagged when the AI makes a logical inference that isn't explicitly stated.
- **Open Questions** are captured to highlight ambiguities and items needing follow-up.

This ensures the generated BRD is a trustworthy starting point, with all ambiguities preserved for human review.

## Architecture and Data Flow

The application uses a modular, multi-source ingestion architecture where any transcript source is first normalized before flowing through the core BRD generation and evidence validation pipeline.

```
                  ┌───────────────────────────────┐
                  │ Transcript Ingestion Sources  │
                  ├───────────────────────────────┤
                  │ 1. Manual Paste               │
                  │ 2. Upload .txt File           │
                  │ 3. Google Meet (Future/OAuth) │
                  │ 4. MS Teams (Future/Graph)    │
                  └───────────────┬───────────────┘
                                  │
                                  ▼
                   Normalized Transcript Model
                                  │
                                  ▼
                       Streamlit Web Interface
                                  │
                                  ▼
                       Python Application Logic
                                  │
                                  ▼
                       Google Gemini API Call
                                  │
                                  ▼
                       Structured JSON Response
                                  │
                                  ▼
                   Python Evidence Validation Layer
                                  │
                                  ▼
                        BRDData Object Creation
                                  │
                                  ▼
                BRD Display in UI & Markdown Export
```

### Supported Ingestion Modes & Status

1. **Manual Paste (Fully Working)**
   - Allows direct copy-pasting of raw meeting notes.
   - Validates non-empty input and normalizes into internal transcript representation.
2. **Upload Transcript File (.txt) (Fully Working)**
   - Supports uploading `.txt` transcript files.
   - Safely decodes text files (UTF-8 with Latin-1 fallback).
   - Validates that non-empty, meaningful content exists and provides in-browser preview.
   - Passes normalized transcript directly to the unified BRD generation pipeline.
3. **Google Meet (Implemented / Optional Real Integration)**
   - Provider adaptor (`GoogleMeetProvider`) handles OAuth 2.0 authorization, token exchange, refresh tokens, Google Meet API v2 transcript fetching, and Google Drive transcript doc plain-text export.
   - **Prerequisites for live Google Meet connection:**
     - Google Cloud Console project with Google Meet API and Google Drive API enabled.
     - OAuth 2.0 Client ID and Secret (`GOOGLE_WORKSPACE_CLIENT_ID`, `GOOGLE_WORKSPACE_CLIENT_SECRET`).
     - Google Workspace domain permissions to record and access transcripts.
4. **Microsoft Teams (Implemented / Optional Real Integration)**
   - Provider adaptor (`MSTeamsProvider`) handles Microsoft Entra ID OAuth 2.0 authorization, token exchange, refresh tokens, Microsoft Graph API transcript retrieval, and WebVTT transcript parsing.
   - **Prerequisites for live Teams connection:**
     - Microsoft Entra ID (Azure AD) App Registration.
     - Microsoft Graph API permissions (`OnlineMeetingTranscript.Read.All`, `OnlineMeetings.Read`, `User.Read`).
     - Microsoft 365 Tenant Admin consent for API access.
     - Azure credentials (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`).

### Generated BRD Sections

The generated Business Requirements Document includes the following structured sections:
1. **Project Overview**: Project name and date.
2. **Business Objectives**: High-level business problem and goals.
3. **Stakeholders**: Names, roles, stated interests, concerns, requests, and responsibilities.
4. **Key Decisions**: Explicitly agreed decisions made during the meeting.
5. **Scope**:
   - **In Scope (Functional & Non-Functional Requirements)**: Each confirmed requirement is strictly grounded with verbatim source evidence quotes.
   - **Deferred or Out of Scope**: Explicitly parked or deferred features.
6. **Acceptance Criteria**: Testable conditions derived from confirmed requirements.
7. **Dependencies & Risks**: Technical/business dependencies and risks or spikes.
8. **Assumptions**: Inferred statements or ungrounded requirements requiring stakeholder confirmation.
9. **Open Questions**: Unresolved ambiguities and items requiring follow-up.
10. **Action Items & Next Steps**: Actionable checklist with assigned owners and deadlines.
11. **Background Information**: Supporting domain and technical context.

### How Source Grounding Works

The "Python Evidence Validation Layer" is the key to the application's reliability.

1.  The Gemini API is instructed to return a `source_evidence` field for every requirement it identifies containing a verbatim quote.
2.  After receiving the API response, the Python backend iterates through each "confirmed" requirement.
3.  It performs a simple but crucial check: `if evidence_string in original_meeting_notes:`.
4.  If the evidence is present in the source text, the requirement is accepted.
5.  If the evidence is missing or doesn't match, the requirement is **re-classified** as an `Assumption`, preventing AI hallucinations from being presented as facts.

## Technology Stack

- **Python 3.11+**
- **Streamlit**: For the interactive web UI.
- **Google Gemini API** (`google-genai`): For natural language processing and structured extraction.
- **requests**: For OAuth token exchanges and REST API integrations (Google Meet & Microsoft Graph).
- **pytest**: For unit testing the core validation logic and ingestion pipelines.

## How to Run the Project Locally

1.  **Clone the repository:**

    ```bash
    git clone <repository-url>
    cd auto-brd-generator
    ```

2.  **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure Environment Variables:**
    - Create a `.env` file in the project's root directory:
      ```env
      # Required for BRD generation
      GEMINI_API_KEY="YOUR_GEMINI_API_KEY"

      # Optional: Google Meet & Workspace Integration
      GOOGLE_WORKSPACE_CLIENT_ID="YOUR_GOOGLE_CLIENT_ID"
      GOOGLE_WORKSPACE_CLIENT_SECRET="YOUR_GOOGLE_CLIENT_SECRET"

      # Optional: Microsoft Teams & Graph Integration
      AZURE_CLIENT_ID="YOUR_AZURE_CLIENT_ID"
      AZURE_TENANT_ID="YOUR_AZURE_TENANT_ID"
      AZURE_CLIENT_SECRET="YOUR_AZURE_CLIENT_SECRET"

      # Optional Redirect URI (default: http://localhost:8501)
      APP_REDIRECT_URI="http://localhost:8501"
      ```
    - The `.gitignore` file is already configured to prevent `.env` from being committed to Git.

4.  **Run the Streamlit application:**

    ```bash
    streamlit run main.py
    ```

5.  **Run the tests:**
    ```bash
    pytest
    ```

## Limitations & Technical Considerations

- **Enterprise Permissions:** Live transcript retrieval from Google Meet or Microsoft Teams requires active enterprise Workspace / Microsoft 365 tenant administrative policies and user consent.
- **Verbatim Evidence Check:** The validation `evidence in notes` checks for exact substrings in the transcript, ensuring no unsupported claims are presented as requirements.
- **Stateless Operation:** The application is stateless; BRD drafts can be downloaded directly as Markdown files.
- **Prompt Sensitivity:** The quality of the output is grounded on the clarity and content of the source notes.

