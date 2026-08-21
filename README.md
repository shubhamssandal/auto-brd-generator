# Auto-BRD Generator from Meeting Notes

This project is a Streamlit web application that converts raw, unstructured meeting notes into a draft Business Requirements Document (BRD). It is designed as a portfolio piece to demonstrate skills in business analysis, requirements engineering, and the responsible application of Large Language Models (LLMs).

## The Problem It Solves

Business Analysts and Product Managers often spend significant time manually sifting through messy meeting notes to extract and structure key decisions, requirements, and action items. This manual process is time-consuming and prone to error. This tool aims to automate the initial draft of a BRD, freeing up the analyst to focus on higher-value tasks like validation, clarification, and strategic planning.

## Core Principle: The Notes Are the Source of Truth

The application's most important feature is its commitment to traceability. It will **never** present an AI-inferred statement as a confirmed fact.

- **Confirmed Requirements** must have direct, verifiable `Source Evidence` from the original notes.
- **Assumptions** are clearly flagged when the AI makes a logical inference that isn't explicitly stated.
- **Open Questions** are captured to highlight ambiguities and items needing follow-up.

The same rule applies to people. A speaker's role is recorded only when the transcript actually states it: `Priya (PM): ...` yields *Priya — PM*, while `Priya: ...` yields *Priya* with no role. Roles, participants, meeting titles and dates are never inferred — when a source does not supply them, the field stays empty and the UI says so.

## Architecture and Data Flow

All four ingestion sources normalize into one `NormalizedTranscript` and then flow through exactly the same generation, validation and export path. There is no per-provider BRD logic.

```
        ┌──────────────────────────────────────────────┐
        │           Transcript Ingestion               │
        ├──────────────────────────────────────────────┤
        │ 1. Manual Paste                              │
        │ 2. Upload .txt File                          │
        │ 3. Google Meet   (OAuth 2.0 + Meet/Drive API)│
        │ 4. Microsoft Teams (Entra ID + Graph API)     │
        └───────────────────────┬──────────────────────┘
                                ▼
                 NormalizedTranscript  (one shared model)
                                ▼
                      Google Gemini API Call
                                ▼
                     Structured JSON Response
                                ▼
              Python Evidence Validation Layer
                                ▼
                      BRDData Object Creation
                                ▼
             BRD Display in UI & Markdown Export
```

### Module map

| File | Responsibility |
| --- | --- |
| `main.py` | Streamlit UI, OAuth callback handling, Gemini call, evidence validation, Markdown export |
| `brd_models.py` | `NormalizedTranscript` and the BRD dataclasses |
| `transcript_processor.py` | Manual-paste and `.txt` upload normalization |
| `providers/base.py` | `TranscriptProvider` contract and the provider error hierarchy |
| `providers/oauth_state.py` | Signed CSRF state + PKCE handshake values |
| `providers/session_tokens.py` | `TokenSet`, expiry-aware refresh, retry-on-401 |
| `providers/google_meet.py` | Google Meet API v2 + Drive export adaptor |
| `providers/ms_teams.py` | Microsoft Graph adaptor and WebVTT parsing |

## Ingestion Modes and Their Real Status

Read this section literally. "Unit-tested with mocked APIs" means the code path is exercised against recorded API shapes, not against a live tenant.

### 1. Manual Paste — working and used end to end

Direct paste of raw notes, validated for non-empty content and normalized before generation.

### 2. Upload Transcript File (.txt) — working and used end to end

`.txt` upload with UTF-8 decoding and a Latin-1 fallback, an in-browser preview, and the same downstream pipeline.

### 3. Google Meet — implemented, unit-tested with mocked APIs, **not verified against a live Google account**

Implemented:

- Full OAuth 2.0 authorization-code flow with a cryptographically signed, single-use `state` (HMAC-SHA256) and PKCE (S256). Denial (`?error=access_denied`), unverifiable state, invalid codes and token-exchange failures are each handled and explained.
- `access_type=offline` + `prompt=consent` so Google issues a refresh token; expiry-aware refresh plus a single retry when the API answers `401`.
- Meeting discovery through Meet API v2 `conferenceRecords` → `transcripts`, plus a Google Drive search for the Google Docs transcript artifacts Meet writes. Both paths are paginated, and anything not retrieved in one pass is reported rather than silently dropped.
- Transcript retrieval from Meet API `transcriptEntries` (fully paginated), with speaker names resolved from the `participants` resource. Participants Google will not name become neutral `Speaker 1` / `Speaker 2` labels — never an invented identity.
- A Drive plain-text export fallback for transcripts whose structured entries are absent but whose Docs artifact exists.
- Distinct messages for: no conference records, no transcript, transcript still recording (`STARTED`), file not generated yet (`ENDED`), unauthorized (`403`), expired token (`401`), not found (`404`), rate limited (`429`), and Drive export failure.

Requirements for a live connection:

- A Google Cloud project with the **Google Meet API** and **Google Drive API** enabled.
- An OAuth 2.0 **Web application** client whose Authorized redirect URI matches `APP_REDIRECT_URI` exactly.
- Scopes: `https://www.googleapis.com/auth/meetings.space.readonly`, `https://www.googleapis.com/auth/drive.readonly`, `openid`, `https://www.googleapis.com/auth/userinfo.email`.
- **Meet transcription is a paid Google Workspace feature** (Business Standard and above, or equivalent) and must have been switched on for the meeting itself. A personal `@gmail.com` account cannot produce Meet transcripts, so OAuth will succeed and discovery will return no transcripts.
- While the OAuth consent screen is in **Testing** mode, only accounts added as test users can sign in.

Known API limits that shape the UI:

- Meet API v2 exposes **no meeting subject or title**, so the meeting list is labelled with the meeting code and start time, and the title column reads "not provided by this API". The Drive path does carry a real document name, so it is shown there.
- Meet warns that `transcriptEntries` "might not match" the Google Docs transcript file exactly.

### 4. Microsoft Teams — implemented, unit-tested with mocked APIs, **not verified against a live Microsoft 365 tenant**

Implemented:

- Microsoft Entra ID authorization-code flow with the same signed state and PKCE, `offline_access` for refresh, and the same expiry-aware refresh and retry-on-`401`.
- Meeting discovery via `GET /me/events` (`isOnlineMeeting eq true`, paginated through `@odata.nextLink`) → each event's `onlineMeeting.joinUrl` → `GET /me/onlineMeetings?$filter=JoinWebUrl eq '...'` → `GET /me/onlineMeetings/{id}/transcripts`. Delegated Graph v1.0 has **no** "list all my online meetings" endpoint, so the join URL is the only supported lookup.
- A manual join-URL fallback for meetings that never appear on the calendar, plus direct `{meetingId}/transcripts/{transcriptId}` entry.
- Content negotiation: speaker-attributed `text/vtt` first; on `403 SpeakerAttributionNotAllowed` (or `406`) it retries with `Accept: application/vnd.microsoft.graph.transcript+text`, which is the documented unattributed type and cannot be requested via `$format`.
- Structural WebVTT parsing that reads speakers from `<v Name>` voice tags, rejoins multi-line cues, merges consecutive cues from one speaker, and strips `WEBVTT`, `NOTE`, timing lines and inline tags.
- Distinct messages for: no online-meeting events, no transcript, `GraphAccessToTranscriptsDisabled` (tenant policy — no application-side workaround), missing permissions / admin consent required, expired meeting, `404`, `429`, network failures and parse failures.

Requirements for a live connection:

- A Microsoft Entra ID **app registration** with a Web redirect URI matching `APP_REDIRECT_URI`, and a client secret.
- **Delegated** Microsoft Graph permissions — this app never uses application-only permissions, and the two are not interchangeable:
  | Permission | Why |
  | --- | --- |
  | `OnlineMeetingTranscript.Read.All` | The only delegated permission Graph offers for reading transcripts. **Requires tenant admin consent.** |
  | `OnlineMeetings.Read` | Resolve an online meeting from its join URL |
  | `Calendars.Read` | Discover which meetings exist (there is no list-meetings endpoint) |
  | `User.Read` | Show which account is signed in |
  | `offline_access` | Obtain a refresh token |
- **Tenant admin consent** for `OnlineMeetingTranscript.Read.All`. Without it, Graph returns `403` and the app says so instead of showing an empty list.
- Transcripts are unavailable for **expired meetings** and for Graph-created meetings that have no calendar event. A tenant admin can also disable Graph transcript access entirely (`GraphAccessToTranscriptsDisabled`).

## Generated BRD Sections

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

1. The Gemini API is instructed to return a `source_evidence` field for every requirement it identifies, containing a verbatim quote.
2. After receiving the API response, the Python backend iterates through each "confirmed" requirement.
3. It performs a simple but crucial check: `if evidence_string in original_transcript_text:`.
4. If the evidence is present in the source text, the requirement is accepted.
5. If the evidence is missing or doesn't match, the requirement is **re-classified** as an `Assumption`, preventing AI hallucinations from being presented as facts.

This check is identical for all four sources. A transcript retrieved from Google Meet or Teams gets no special treatment, which is why provider transcript text is preserved verbatim rather than reformatted.

## Credential and Token Handling

- All credentials come from environment variables loaded from `.env`. Nothing is hardcoded.
- `.env` is git-ignored (along with `brd-env/` and `.streamlit/secrets.toml`). Only `.env.example`, which contains variable **names** and no values, is tracked.
- Credential values are never printed, logged, written to this README, or rendered in the UI. When a provider is unconfigured the app lists the missing variable **names** only.
- Access and refresh tokens live only in Streamlit's server-side session state for the duration of the session. They are never displayed, logged, or written to disk, and `TokenSet.__repr__` redacts them so they cannot leak through a traceback. The UI shows only token type, granted scopes, remaining lifetime, and whether renewal is available.
- Provider error messages are built from the API's own error text with a 400-character cap and carry no credential material.
- The OAuth `state` is an HMAC-signed, TTL-bounded, single-use value rather than a guessable constant, and the PKCE verifier is derived from the signing key plus a public nonce so it never travels in a URL.

## Technology Stack

- **Python 3.9+** — developed and tested on Python 3.9.6 (`./brd-env`). The code deliberately avoids `X | Y` type unions so it runs on 3.9.
- **Streamlit** 1.35.0 — interactive web UI.
- **Google Gemini API** (`google-genai` 1.47.0) — natural language processing and structured extraction.
- **requests** 2.32.5 — OAuth token exchanges and REST calls to Google Meet, Google Drive and Microsoft Graph. No OAuth SDK is required.
- **pytest** 8.2.2 — unit tests.

## How to Run the Project Locally

1. **Clone the repository:**

   ```bash
   git clone <repository-url>
   ```

2. **Create a virtual environment and install dependencies:**

   ```bash
   python3 -m venv brd-env && ./brd-env/bin/pip install -r requirements.txt
   ```

3. **Configure environment variables:** copy the template and fill in your own values.

   ```bash
   cp .env.example .env
   ```

   Only `GEMINI_API_KEY` is required. The Google and Microsoft variables are optional — leave them unset and those two sources simply report **Provider Not Configured** while Manual Paste and TXT Upload keep working normally. See `.env.example` for the full list of names.

4. **Run the app:**

   ```bash
   ./brd-env/bin/streamlit run main.py
   ```

5. **Connect a provider (optional):** choose *Google Meet* or *Microsoft Teams*, click **Connect**, authorize on the provider's own sign-in page, and you are returned to the app. Then use **Retrieve available meetings & transcripts**, pick a meeting from the list, **Load transcript**, review the preview, and press **Generate BRD from this transcript**.

## How to Test

```bash
./brd-env/bin/pytest -v
```

**Last run: 135 passed.** Every external API call is monkeypatched, so no Google, Microsoft or Gemini credentials are needed to run the suite and no network access is required.

| File | Covers |
| --- | --- |
| `test_main.py` | Evidence validation and re-classification, BRD assembly, Markdown formatting |
| `test_transcript_ingestion.py` | Manual/upload normalization, provider configuration reporting, token exchange, VTT parsing, four-source pipeline |
| `test_oauth_security.py` | Signed state round-trip, forged/tampered/expired/wrong-provider state, PKCE S256 derivation, `TokenSet` expiry and redaction, proactive and reactive refresh |
| `test_providers_google.py` | Authorization URL, PKCE verifier submission, HTTP→error mapping, pagination and truncation reporting, discovery, participant-name resolution, `Speaker N` placeholders, Drive fallback, refresh-and-retry |
| `test_providers_teams.py` | Least-privileged delegated scopes, join-URL resolution, calendar discovery and `@odata.nextLink` paging, VTT speaker parsing, `SpeakerAttributionNotAllowed` fallback, `GraphAccessToTranscriptsDisabled`, admin-consent `403`, refresh-and-retry |
| `test_main_integration.py` | OAuth denial, forged-state rejection, verified callback, missing refresh token, exchange failure, disconnect, additive `NormalizedTranscript` fields, provider transcripts through the shared validation pipeline, no invented stakeholder roles |

## What Has Actually Been Verified

| Area | Status |
| --- | --- |
| Manual Paste → BRD → Markdown export | Working; exercised in the app and covered by tests |
| TXT Upload → BRD → Markdown export | Working; exercised in the app and covered by tests |
| Evidence validation and demotion to Assumption | Working; covered by tests |
| Google Meet / Teams provider code paths | Implemented and covered by tests against mocked API responses |
| OAuth state, PKCE, token refresh and retry logic | Covered by tests |
| **Live Google Meet transcript retrieval** | **Not verified.** Requires real `GOOGLE_WORKSPACE_*` credentials and a Google Workspace account with Meet transcription enabled. |
| **Live Microsoft Teams transcript retrieval** | **Not verified.** Requires a real Entra ID app registration, `AZURE_*` credentials, and tenant admin consent. |

## Limitations and Technical Considerations

- **Live provider retrieval is unverified.** The Google and Microsoft code paths follow the published API contracts and are tested against mocked responses, but no live retrieval has been performed. Treat those two sources as ready to test, not as proven.
- **Google Meet requires paid Workspace transcription.** Without it, OAuth succeeds and the meeting list comes back empty — which is a correct result, not a bug.
- **Teams requires tenant admin consent** for `OnlineMeetingTranscript.Read.All`, and a tenant admin can disable Graph transcript access outright. Neither can be worked around from the application side.
- **No meeting title from Meet API v2.** The Meet list is labelled by meeting code and time; titles appear only for the Drive document path.
- **Discovery is bounded and says so.** To avoid long scans, the most recent conference records / calendar events are probed, and anything skipped is reported in the UI. Nothing is truncated silently.
- **Verbatim evidence check.** Validation uses exact substring containment, so a paraphrased quote is demoted to an Assumption even when the underlying point is real.
- **Stateless operation.** Nothing is persisted between sessions; tokens vanish when the session ends and BRD drafts are downloaded as Markdown.
- **OAuth state does not survive a restart** unless `OAUTH_STATE_SECRET` is set, because the signing key is otherwise generated per process. An in-flight sign-in must be restarted.
- **Prompt sensitivity.** Output quality is bounded by the clarity and completeness of the source notes.
