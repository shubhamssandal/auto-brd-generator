# AI-Assisted Software Delivery Lifecycle

This project is a Streamlit web application for an AI-assisted software delivery lifecycle:

> **Discovery → BRD → PRD → Architecture → Implementation Plan → Sprint Planning → Test Cases → Test Execution → Jira / Delivery Status**

Today it converts raw, unstructured meeting notes into a draft Business Requirements Document (BRD), derives a Product Requirements Document (PRD) from an approved BRD, derives a technical architecture from an approved PRD, decomposes the approved PRD and architecture into an implementation plan of epics, stories and technical tasks, and delivers either the reviewed requirements or an approved implementation plan into Jira as traceable issues. The remaining stages — sprint planning, test cases and test execution — are **navigable in the UI but not implemented yet**; each one reports its own state rather than offering a control that does nothing. See [BRD to PRD](#brd-to-prd), [PRD to Architecture](#prd-to-architecture), [Architecture to Implementation Plan](#architecture-to-implementation-plan), [Implementation Plan to Jira](#implementation-plan-to-jira) and [What Has Actually Been Verified](#what-has-actually-been-verified) for exactly what is built and what is planned.

It is designed as a portfolio piece to demonstrate skills in business analysis, requirements engineering, and the responsible application of Large Language Models (LLMs).

## The Problem It Solves

Business Analysts and Product Managers often spend significant time manually sifting through messy meeting notes to extract and structure key decisions, requirements, and action items. This manual process is time-consuming and prone to error. This tool aims to automate the initial draft of a BRD, freeing up the analyst to focus on higher-value tasks like validation, clarification, and strategic planning.

## Core Principle: The Notes Are the Source of Truth

The application's most important feature is its commitment to traceability. It will **never** present an AI-inferred statement as a confirmed fact.

- **Confirmed Requirements** must have direct, verifiable `Source Evidence` from the original notes.
- **Assumptions** are clearly flagged when the AI makes a logical inference that isn't explicitly stated.
- **Open Questions** are captured to highlight ambiguities and items needing follow-up.

The same rule applies to people. A speaker's role is recorded only when the transcript actually states it: `Priya (PM): ...` yields _Priya — PM_, while `Priya: ...` yields _Priya_ with no role. Roles, participants, meeting titles and dates are never inferred — when a source does not supply them, the field stays empty and the UI says so.

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
                                ▼
        Explicit BRD approval → PRD generation  (+ optional
        product-refinement transcript) → review/edit → approval
                                ▼
        Explicit PRD approval → Architecture generation  (+ optional
        architecture discussion) → review/edit → approval
                                ▼
        Explicit architecture approval → Implementation Plan
        generation  (epics → stories → technical tasks)
                 → review/edit → approval
                                ▼
        Optional Jira delivery  (OAuth 2.0 3LO + Atlassian REST)
        site → project → required fields → work plan → review
                 → explicit confirmation → issue creation
                                ▼
        Approved implementation plan → mapped onto that project's
        own issue types → preview → explicit confirmation
                 → Epics / Stories / Tasks / Subtasks
                 → plan item → issue key mapping
```

Everything after the Markdown export is optional. A BRD is complete without it, and nothing is written to Jira until a reviewer confirms. The Jira panel now carries two independent deliveries into the same project: the reviewed **BRD** as issues, and an **approved implementation plan** as an Epic → Story → Task hierarchy. Neither is a prerequisite of the other, and no Jira read of any kind changes the BRD, the PRD, the architecture or the plan — the trail runs one way.

### Module map

| File                          | Responsibility                                                                                                            |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `main.py`                     | Streamlit UI, OAuth callback handling, Gemini call, evidence validation, Markdown export, Jira panel, lifecycle workspace |
| `lifecycle_models.py`         | Delivery lifecycle stages, states, and `lifecycle_from` derivation from session artifacts                                 |
| `brd_models.py`               | `NormalizedTranscript` and the BRD dataclasses                                                                            |
| `prd_models.py`               | PRD dataclasses: `Feature`, `Persona`, `UserJourney`, `PRDData`, and BRD-requirement traceability                          |
| `prd_generator.py`            | Approved BRD → PRD prompt, untrusted-response validation, deterministic baseline fallback                                  |
| `architecture_models.py`      | Architecture dataclasses: `Component`, `Decision`, `DataFlow`, `Integration`, `Risk`, `ArchitectureData`, PRD-feature traceability |
| `architecture_generator.py`   | Approved PRD → architecture prompt, untrusted-response validation, deterministic baseline fallback                          |
| `implementation_plan_models.py`    | Implementation plan dataclasses: `Epic`, `Story`, `TechnicalTask`, `ImplementationPlan`, priority vocabulary, PRD-feature and architecture-component traceability |
| `implementation_plan_generator.py` | Approved PRD + architecture → plan prompt, untrusted-response validation, deterministic repair (unknown ids, dependency cycles), baseline fallback |
| `implementation_plan_jira.py`      | Approved plan → one project's own issue types, hierarchy-shortfall reporting, the stable plan item → issue key mapping, creation progress |
| `model_output.py`             | Shared reader for untrusted model output: JSON extraction, field aliases, safe error messages                              |
| `transcript_processor.py`     | Manual-paste and `.txt` upload normalization                                                                              |
| `providers/base.py`           | `TranscriptProvider` contract and the provider error hierarchy                                                            |
| `providers/oauth_state.py`    | Signed CSRF state + PKCE handshake values                                                                                 |
| `providers/session_tokens.py` | `TokenSet`, expiry-aware refresh, retry-on-401                                                                            |
| `providers/google_meet.py`    | Google Meet API v2 + Drive export adaptor                                                                                 |
| `providers/ms_teams.py`       | Microsoft Graph adaptor and WebVTT parsing                                                                                |
| `jira_config.py`              | Jira client configuration read from the environment                                                                       |
| `jira_service.py`             | Jira OAuth 2.0 (3LO), accessible sites, projects, create-metadata, issue creation                                         |
| `jira_models.py`              | `PlannedIssue`, `JiraWorkPlan`, `CreatedIssue`                                                                            |
| `jira_processor.py`           | Deterministic BRD → work-plan mapping, plan validation, ADF conversion                                                    |
| `jira_planner.py`             | Optional AI hierarchy planning, validated deterministically before use                                                    |

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

## BRD to PRD

An **approved** BRD can be turned into a Product Requirements Document from the "Product Definition → PRD" stage in the project workspace. The PRD is a different document, not a restatement: it adds a product overview, goals, personas, features, user journeys, functional behaviour, edge cases, acceptance criteria, product assumptions, open questions and — only where the material supports them — success metrics.

- **The approved BRD is the input.** No second transcript is required. A product-refinement discussion can optionally be pasted in (or reused from a transcript already loaded in the session) to enrich the result, and the source it came from is recorded on the PRD.
- **Approval gates the stage.** The stage offers no generation control until the BRD has been approved through its own button, and generating a PRD leaves it at *Pending Review*. Editing it keeps it there. Only the "Approve PRD" button moves it to *Approved*.
- **Traceability is structural.** Every feature and journey names the BRD requirement ids it serves, reusing the ids already in the BRD. Requirement ids the BRD does not hold are removed, a feature that names none is dropped, and uncovered BRD requirements are reported in the PRD's notes rather than hidden.
- **Failure degrades instead of inventing.** With no model configured, an unreachable provider, or a response that cannot be read, the stage falls back to a deterministic one-feature-per-requirement draft and says why in a note. Provider failures report the exception type only, never the client's message.
- **A new BRD invalidates what came from the old one.** Regenerating or re-storing a BRD clears its approval, the PRD, the PRD's approval and the review editor's state.

## PRD to Architecture

An **approved** PRD can be turned into a technical architecture from the "Architecture" stage in the project workspace. The architecture is a translation of the PRD, not a restatement: it names the backend services, web application areas and mobile modules that realise the product, with each component's responsibility, API boundaries, owned data and dependencies, plus core domains, authentication and authorization, system data flows, external integrations, architecture decisions, technical dependencies and technical risks.

- **The approved PRD is the input.** No transcript is required. An architecture or design discussion can optionally be pasted in to add technical evidence, and the source it came from is recorded on the architecture.
- **Approval gates the stage.** The stage offers no generation control until the PRD has been approved through its own button, and generating an architecture leaves it at *Pending Review*. Editing it keeps it there. Only the "Approve architecture" button moves it to *Approved*.
- **Traceability is structural, and cross-cutting design survives it.** Components, decisions, flows and integrations name the PRD feature ids they realise, reusing the ids already in the PRD. Feature ids the PRD does not hold are removed. Unlike a PRD feature, a component that names no feature is **kept** — a gateway or shared auth service is genuinely cross-cutting — and counted in the architecture's notes so a reviewer sees it. Uncovered PRD features and a layer with no component are reported the same way.
- **A component's layer comes from the section it arrived in**, not from anything the model asserts about itself. Only a component in a generic list has its layer inferred, and that inference is noted.
- **Failure degrades instead of inventing.** With no model configured, an unreachable provider, or a response that cannot be read, the stage falls back to a deterministic skeleton — one component per PRD feature in each layer, with no decisions, flows or risks — and says why in a note. Provider failures report the exception type only, never the client's message.
- **A new PRD invalidates the architecture designed against the old one.** Regenerating the PRD clears the architecture, its approval and the review editor's state.

## Architecture to Implementation Plan

An **approved** architecture, standing on an approved PRD and BRD, can be decomposed into an implementation plan from the "Implementation Plan" stage. This is the first artifact shaped like work rather than like a document: it is what gets built, in what order, and how anyone knows it is done.

- **Three levels, and they are not interchangeable.** An **epic** is a product capability that groups stories and is not itself executable. A **story** is a user or product outcome with acceptance criteria that decide whether it was achieved. A **task** or **subtask** is technical implementation work. Technical work is deliberately not forced into a user story — a migration, a CI pipeline or an SDK upgrade is a task, because "as a user I want a migration" is a lie about who wants it.
- **Both approved artifacts are the input.** No transcript is involved. The stage offers no generation control until the architecture, the PRD and the BRD have each been approved through their own buttons, and generating a plan leaves it at *Pending Review*. Editing it keeps it there. Only the "Approve implementation plan" button moves it to *Approved*.
- **Traceability is asymmetric, on purpose.** A story names the PRD feature ids it delivers and the architecture component ids it touches, reusing ids already minted upstream. A story that names no feature is **dropped** as invented scope, and the note tells the reviewer to re-add it as a task if it was real technical work. A *task* that names no component is **kept** — repository setup or a release step genuinely touches no single component. Uncovered PRD features and unbuilt architecture components are reported separately, because an unbuilt component means the design promises something the plan does not deliver.
- **A proposal is repaired deterministically, never re-prompted.** Feature and component ids the upstream artifacts do not hold are removed; stories naming an unknown epic are kept but left ungrouped; self-dependencies and dependencies matching no story are dropped; a dependency cycle loses only its closing link, so the work stays startable; priorities outside `Highest/High/Medium/Low/Lowest` are set to `Medium`. Every repair is listed in the plan's notes, so a reviewer reads what changed instead of trusting the result.
- **Readiness is reported, not faked.** A story missing a description, acceptance criteria, technical tasks, a component or a feature is flagged with the specific gaps rather than having them auto-filled — those are judgement calls a product owner makes.
- **Failure degrades instead of inventing.** With no model configured, an unreachable provider, a response that cannot be read, or a response whose every story was untraceable, the stage falls back to a deterministic skeleton — one epic and story per PRD feature, one task per realising component, borrowing the PRD's own acceptance criteria and proposing no sequencing it cannot justify — and says why. Provider failures report the exception type only, never the client's message.
- **The plan itself is tracker-agnostic.** It exists before any tracker is chosen, so it carries no Jira issue types, hierarchy levels or required fields. Approving it writes nothing anywhere; delivering it into a project is the separate, explicitly confirmed step described in [Implementation Plan to Jira](#implementation-plan-to-jira).
- **A new architecture invalidates the plan built against the old one.** Regenerating the PRD or the architecture clears the plan, its approval and the review editor's state.

## Implementation Plan to Jira

An **approved** implementation plan can be delivered into the selected Jira project as Step 6 of the Jira panel. It is the only stage that writes work items to a tracker, and it is built so that a retry after something goes wrong finishes the job instead of doubling it.

- **Approval is the gate.** A plan that is merely generated creates nothing: the panel says so and offers no button. Nothing about the plan, the architecture, the PRD or the BRD is ever modified by anything Jira reports — the trail runs one way, and no BRD synchronization exists in either direction.
- **The project's hierarchy decides the issue types.** `implementation_plan_jira.map_plan_to_work_plan` reads the rungs Jira actually reported for that project and places an epic on the top rung, a story on the next and a technical task on the one below. A project whose rungs are named `Initiative / Feature / Work Item` is served in its own vocabulary, and the substitution is stated in a note rather than being silent.
- **A hierarchy Jira cannot represent is reported, not faked.** If the project has two rungs and the plan needs three, the items that do not fit are shown **unselected**, with a note naming the shortfall, and nothing is flattened onto the wrong parent or promoted into the wrong type. A story whose epic could not be created is created without a parent and keeps its plan epic id in the description, because the work is real even when its container is not creatable.
- **Plan ids and upstream references travel with the issue.** Each issue's description carries its own plan id, the PRD feature ids and architecture component ids it traces to, its dependencies, priority and estimate, and the titles of the PRD and architecture it descends from — so a reviewer reading the issue in Jira can get back to the artifact it came from.
- **The mapping is stable and idempotent.** A `DeliveryMapping` records plan item id → Jira issue key for every item that was actually created. Re-rendering the panel, editing the plan or running again does not move a key. Because Jira's create endpoint has no idempotency key, a repeat is prevented *before* the request: an item that already has a key is skipped, while its key is still used as the parent of a child created now. A failure is deliberately **not** recorded as a link, so the item stays pending and a retry creates it.
- **A partial failure is recoverable.** A run stops at the first failure rather than orphaning children, folds its successes into the mapping, and shows what is left. The next run creates only the pending items.
- **Writing needs two explicit confirmations.** A request button, then a warning naming the count and the project, then "Yes — create them now". An in-flight guard stops a rerun from starting a second run, and a session authorized without `write:jira-work` is told before anything is attempted.
- **Status means creation evidence.** The delivery panel and the "Jira / Delivery Status" stage report what this app recorded when it created each issue, with a browse link per issue. This app has **no** read-issue endpoint and does not poll an issue's workflow state — the per-issue read that once existed was removed with the requirement-drift feature it served, and reintroducing it is out of scope.

## Optional Jira Delivery (the BRD path)

A reviewed BRD can be turned into Jira issues without leaving the app. The panel appears under the BRD and takes five explicit steps: choose a Jira site, choose a project, check what that project's issue types actually require, generate a work plan, and create the selected issues.

- **Connection.** Jira uses the same OAuth 2.0 session and token handling as the transcript providers: a signed single-use `state`, expiry-aware refresh, and one retry on `401`. PKCE is deliberately not sent, because Atlassian's 3LO flow documents only `audience`, `client_id`, `scope`, `redirect_uri`, `state`, `response_type` and `prompt`. The requested scopes are `read:me`, `read:jira-user`, `read:jira-work`, `write:jira-work` and `offline_access` — the write scope only because issue creation needs it.
- **Work plan.** `jira_processor.build_work_plan` maps confirmed requirements and action items onto the project's real issue types. Hierarchy grouping may be proposed by Gemini through `jira_planner`, but the model's output is treated as untrusted: `validate_work_plan` rejects unknown issue types, invalid parent/child levels, self-parents, cycles, duplicate keys and invalid subtasks, and the deterministic mapping is used when the model is unavailable or its answer fails validation.
- **Review before writing.** Every proposed issue is editable and individually selectable, and creation is behind a two-step confirmation. A page load, a rerun or a regenerated plan sends no request.
- **Creation and traceability.** Issues are created parents-first so children can reference real keys. Each result is stored as a `CreatedIssue` linking `issue_key` back to the `plan_key`, the source requirement ids and the source action-item ids — preserving full traceability from Jira back to the originating BRD requirement.

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
- **requests** 2.32.5 — OAuth token exchanges and REST calls to Google Meet, Google Drive, Microsoft Graph and the Atlassian/Jira Cloud REST API v3. No OAuth or Jira SDK is required.
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

   Only `GEMINI_API_KEY` is required. The Google, Microsoft and Jira variables are optional — leave them unset and those integrations simply report **Provider Not Configured** while Manual Paste and TXT Upload keep working normally. See `.env.example` for the full list of names.

4. **Run the app:**

   ```bash
   ./brd-env/bin/streamlit run main.py
   ```

5. **Connect a provider (optional):** choose _Google Meet_ or _Microsoft Teams_, click **Connect**, authorize on the provider's own sign-in page, and you are returned to the app. Then use **Retrieve available meetings & transcripts**, pick a meeting from the list, **Load transcript**, review the preview, and press **Generate BRD from this transcript**.

6. **Deliver to Jira (optional):** with a BRD on screen, connect Jira (`JIRA_CLIENT_ID` / `JIRA_CLIENT_SECRET` required), pick a site and project, read the required-field report, **Generate Jira Work Plan**, edit and select the issues you want, then confirm creation.

## How to Test

```bash
./brd-env/bin/pytest -q
```

**Last run: 569 passed.** Every external API call is monkeypatched, so no Google, Microsoft, Atlassian or Gemini credentials are needed to run the suite and no network access is required. A test-wide fixture also blanks the Gemini client, so no test can reach a live model.

| File                            | Covers                                                                                                                                                                                                                                            |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_main.py`                  | Evidence validation and re-classification, BRD assembly, Markdown formatting                                                                                                                                                                      |
| `test_transcript_ingestion.py`  | Manual/upload normalization, provider configuration reporting, token exchange, VTT parsing, four-source pipeline                                                                                                                                  |
| `test_oauth_security.py`        | Signed state round-trip, forged/tampered/expired/wrong-provider state, PKCE S256 derivation, `TokenSet` expiry and redaction, proactive and reactive refresh                                                                                      |
| `test_providers_google.py`      | Authorization URL, PKCE verifier submission, HTTP→error mapping, pagination and truncation reporting, discovery, participant-name resolution, `Speaker N` placeholders, Drive fallback, refresh-and-retry                                         |
| `test_providers_teams.py`       | Least-privileged delegated scopes, join-URL resolution, calendar discovery and `@odata.nextLink` paging, VTT speaker parsing, `SpeakerAttributionNotAllowed` fallback, `GraphAccessToTranscriptsDisabled`, admin-consent `403`, refresh-and-retry |
| `test_main_integration.py`      | OAuth denial, forged-state rejection, verified callback, missing refresh token, exchange failure, disconnect, additive `NormalizedTranscript` fields, provider transcripts through the shared validation pipeline, no invented stakeholder roles  |
| `test_jira_config.py`           | Jira configuration read from the environment and unconfigured reporting by variable name                                                                                                                                                          |
| `test_jira_oauth.py`            | Jira authorization URL, audience and scopes, token exchange, refresh and error mapping                                                                                                                                                            |
| `test_jira_sites.py`            | Accessible-resources reads, site selection and state clearing                                                                                                                                                                                     |
| `test_jira_projects.py`         | Project discovery, paging, issue types and create-metadata reporting                                                                                                                                                                              |
| `test_jira_work_plan.py`        | Deterministic BRD → work-plan mapping, requirement and action-item traceability, plan validation                                                                                                                                                  |
| `test_jira_planner.py`          | AI hierarchy planning treated as untrusted: malformed output, invalid hierarchies, cycles, and fallback to the deterministic mapping                                                                                                              |
| `test_jira_work_plan_review.py` | Plan editing, per-issue selection, review-state invalidation when the plan or target changes                                                                                                                                                      |
| `test_jira_creation.py`         | Confirmation gating, parents-before-children ordering, partial-failure reporting, `CreatedIssue` mapping, no request on a rerun                                                                                                                   |
| `test_lifecycle_workspace.py`   | Lifecycle stage ordering, state vocabulary enforcement, derived status from session artifacts, workspace rendering, JIRA-010 removal assertions                                                                                                   |
| `test_prd.py`                   | Approved BRD → PRD generation, the approval gate, optional refinement transcript, BRD → PRD traceability, review/edit persistence, explicit approval, provider and malformed-response fallbacks                                                    |
| `test_architecture.py`          | Approved PRD → architecture generation, the PRD gate, backend/web/mobile coverage, optional architecture discussion, PRD → architecture traceability, layer resolution, review/edit persistence, explicit approval, provider and malformed-response fallbacks |
| `test_implementation_plan.py`   | Approved PRD + architecture → implementation plan, both gates, epic/story/task hierarchy and work types, asymmetric traceability (untraceable story dropped, componentless task kept), deterministic repair of unknown ids, self- and dangling dependencies, dependency cycles and priorities, readiness gaps, dependency ordering, baseline fallback paths, domain independence, session cascade |
| `test_implementation_plan_jira.py` | Approval gate, mapping onto the project's own issue types and hierarchy, unrepresentable-hierarchy reporting, preserved Epic → Story → Task parents, plan id and PRD/architecture references on each issue, the stable plan id → issue key mapping, idempotent retry and partial-failure recovery through the real creation loop, the two-step confirmation gate, write-scope refusal, status read from creation evidence, no read-issue endpoint, BRD-path regression, domain independence |

## What Has Actually Been Verified

| Area                                                                  | Status                                                                                                                                                                             |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Manual Paste → BRD → Markdown export                                  | Working; exercised in the app and covered by tests                                                                                                                                 |
| TXT Upload → BRD → Markdown export                                    | Working; exercised in the app and covered by tests                                                                                                                                 |
| Evidence validation and demotion to Assumption                        | Working; covered by tests                                                                                                                                                          |
| Google Meet / Teams provider code paths                               | Implemented and covered by tests against mocked API responses                                                                                                                      |
| OAuth state, PKCE, token refresh and retry logic                      | Covered by tests                                                                                                                                                                   |
| Jira work-plan generation, validation, review and confirmation gating | Covered by tests; no network involved in building or reviewing a plan                                                                                                              |
| Lifecycle workspace: all 8 stages, derived status, no JIRA-010 drift  | Covered by `test_lifecycle_workspace.py`                                                                                                                                           |
| Approved BRD → PRD, traceability, review/edit and explicit approval   | Covered by `test_prd.py`; the model is injected as a callable, so no PRD test reaches a provider                                                                                    |
| Approved PRD → Architecture, traceability, review/edit and approval   | Covered by `test_architecture.py`; the model is injected as a callable, so no architecture test reaches a provider                                                                  |
| Approved architecture → Implementation Plan, hierarchy, repair and approval | Covered by `test_implementation_plan.py`; the model is injected as a callable, so no plan test reaches a provider, and no plan test writes to Jira                             |
| Approved Implementation Plan → Jira issues, stable mapping, idempotent retry | Covered by `test_implementation_plan_jira.py` against a recording service, so no test reaches Atlassian. Delivery status is read from creation records; **no Jira workflow-status polling exists** |
| **Live Google Meet transcript retrieval**                             | **Not verified.** Requires real `GOOGLE_WORKSPACE_*` credentials and a Google Workspace account with Meet transcription enabled.                                                   |
| **Live Microsoft Teams transcript retrieval**                         | **Not verified.** Requires a real Entra ID app registration, `AZURE_*` credentials, and tenant admin consent.                                                                      |
| **Live Jira issue creation**                                          | **Not verified in this repository.** All Jira tests run against mocked Atlassian responses; a real end-to-end run needs a Jira Cloud site and an app with the five scopes granted. |

## Limitations and Technical Considerations

- **Live provider retrieval is unverified.** The Google and Microsoft code paths follow the published API contracts and are tested against mocked responses, but no live retrieval has been performed. Treat those two sources as ready to test, not as proven.
- **Google Meet requires paid Workspace transcription.** Without it, OAuth succeeds and the meeting list comes back empty — which is a correct result, not a bug.
- **Teams requires tenant admin consent** for `OnlineMeetingTranscript.Read.All`, and a tenant admin can disable Graph transcript access outright. Neither can be worked around from the application side.
- **No meeting title from Meet API v2.** The Meet list is labelled by meeting code and time; titles appear only for the Drive document path.
- **Discovery is bounded and says so.** To avoid long scans, the most recent conference records / calendar events are probed, and anything skipped is reported in the UI. Nothing is truncated silently.
- **Verbatim evidence check.** Validation uses exact substring containment, so a paraphrased quote is demoted to an Assumption even when the underlying point is real.
- **Stateless operation.** Nothing is persisted between sessions; tokens vanish when the session ends and BRD drafts are downloaded as Markdown.
- **A stale plan is tolerated, not corrected.** After a BRD is regenerated, a previously generated work plan still restates the older wording until it is regenerated.
- **The lifecycle stops at Jira delivery.** Nothing consumes the created issues yet — sprint planning, test cases and test execution are not implemented — and none of the PRD, the architecture or the plan has a Markdown export of its own, so all three live only in the session.
- **Delivery status is creation evidence, not Jira workflow status.** The app records what it created and links to each issue, but it never reads an issue back, so a `To Do → In Progress → Done` transition in Jira is invisible here. That is deliberate: a per-issue read is what the removed requirement-drift feature used, and reintroducing it would rebuild a Jira → BRD path the product direction forbids.
- **A delivery mapping belongs to one project.** Its keys mean nothing elsewhere, so choosing a different project or disconnecting drops it along with the rest of that project's state, and the plan would be delivered afresh.
- **OAuth state does not survive a restart** unless `OAUTH_STATE_SECRET` is set, because the signing key is otherwise generated per process. An in-flight sign-in must be restarted.
- **Prompt sensitivity.** Output quality is bounded by the clarity and completeness of the source notes.
