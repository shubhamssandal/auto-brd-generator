# Auto-BRD Generator — Technical Architecture

## Architecture Overview
The application is a Streamlit-based Python application that processes transcript input and generates structured BRD output. External transcript providers use a provider-oriented architecture.

## Known Current Components
- `main.py`
- `brd_models.py`
- `transcript_processor.py`
- `providers/base.py`
- `providers/google_meet.py`
- `providers/ms_teams.py`
- `providers/oauth_state.py`
- `providers/session_tokens.py`
- `requirements.txt`

Existing tests include provider, OAuth/security, transcript-ingestion, transcript-processing, main, and integration coverage.

## High-Level Flow
Transcript Source
    ↓
Manual Paste / File Upload / Google Meet / Microsoft Teams
    ↓
Provider or Input Handling
    ↓
Transcript Processing
    ↓
BRD Models / BRD Generation
    ↓
Reviewable BRD Output
    ↓
Jira Integration

## Provider Architecture
External meeting providers should follow the existing provider pattern rather than placing provider logic directly inside `main.py`.

Provider responsibilities:
- Authorization initiation where applicable
- OAuth callback handling
- Token refresh handling
- Transcript discovery
- Transcript retrieval
- Provider-specific errors

## Authentication and Session State
Existing OAuth/session functionality uses dedicated modules for OAuth state and session tokens.

Rules:
- Secrets remain in environment configuration
- OAuth state is validated
- Tokens are not rendered in the UI
- Tokens are not logged

## Jira Integration
Add Jira as a separate integration layer without rewriting stable transcript functionality.

Recommended responsibilities:

`jira_models.py`
- Jira site/project models
- Metadata models
- Work-plan models
- Planned issue models
- Creation-result models

`jira_service.py`
- OAuth
- Token refresh
- Accessible-resource discovery
- Project/metadata retrieval
- Issue creation
- Error normalization

`jira_processor.py`
- BRD-to-Jira work-plan transformation
- Requirement-to-issue mapping
- Validation before creation

## Jira Flow
Connect Jira
→ OAuth Callback
→ Token Exchange
→ Accessible Site Discovery
→ Project Selection
→ Metadata Discovery
→ BRD to Jira Plan
→ User Review
→ Validated Issue Creation
→ Creation Result Mapping

## Data and Side-Effect Boundary
Generation:
BRD → Proposed Jira Work Plan

External side effect:
Approved Plan → Jira API → Created Issues

Generating a plan must never create issues.

## Error Handling
Distinguish between authentication failure, authorization failure, expired/refresh failure, no accessible site/project, missing fields, invalid hierarchy, partial creation failure, and network/API failure.

## Testing Strategy
Test OAuth/state generation, refresh, resource discovery, project discovery, metadata validation, plan validation, confirmation boundary, parent/child creation ordering, partial failures, and regressions.

## Constraints
- Do not rewrite stable Google Meet/Teams code unnecessarily.
- Implement one feature ticket at a time.
- Keep boundaries clear.
- Prefer small, testable functions.
