# Graph Report - Auto BRD  (2026-08-26)

## Corpus Check
- 41 files · ~70,816 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1429 nodes · 3494 edges · 80 communities (71 shown, 9 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 214 edges (avg confidence: 0.95)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `b8aed30e`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- jira_planner.py
- test_main_integration.py
- test_jira_planner.py
- test_jira_work_plan.py
- main.py
- TokenSet
- FakeResponse
- test_lifecycle_workspace.py
- test_transcript_ingestion.py
- test_jira_config.py
- begin_handshake
- PROJECT_CONTEXT.md
- test_providers_teams.py
- jira_processor.py
- a_plan
- test_jira_projects.py
- TranscriptProvider
- MSTeamsProvider
- GoogleMeetProvider
- test_jira_oauth.py
- _skey
- test_jira_work_plan_review.py
- a_plan
- JiraService
- test_providers_google.py
- AI-Assisted Software Delivery Lifecycle
- FakeResponse
- NormalizedTranscript
- Auto-BRD → AI Software Delivery Lifecycle
- .refresh_access_token
- Router
- Auto-BRD Generator — Product Requirements Document (PRD)
- jira_tokens
- .get_project_metadata
- a_complex_brd
- _render_jira_work_plan_panel
- patch_post
- parse_planner_response
- .refresh_access_token
- test_jira_creation.py
- Auto-BRD Generator — Feature Ticket List
- JiraSite
- _create_selected_issues
- ._transcript_from_meet_entries
- Auto-BRD Generator — Technical Architecture
- Auto-BRD Generator — Frontend Specification
- generate_work_plan
- Auto-BRD Generator — Security & Access
- CreatedIssue
- PlannedIssue
- a_type
- Any
- JiraIssueType
- JiraProject
- issue_creation_payload
- creation_order
- delete_planned_issue
- .create_issue
- parametrize
- PlanLevel
- TranscriptUnavailableError
- clean_session
- no_live_model_calls
- .parse_vtt
- .is_expired
- clean_env
- JiraField
- test_all_four_sources_unified_pipeline
- generate_brd_from_notes
- test_ms_teams_list_transcripts
- RecordingGet
- _requirement_description
- StubJira
- no_http_at_all
- test_the_plan_is_cleared_by_every_path_that_invalidates_it
- rules/graphify.md
- workflows/graphify.md
- _names_requirement
- test_an_unreported_scope_list_does_not_block_creation
- provider

## God Nodes (most connected - your core abstractions)
1. `_skey()` - 85 edges
2. `JiraService` - 60 edges
3. `FakeResponse` - 58 edges
4. `MSTeamsProvider` - 57 edges
5. `a_brd()` - 54 edges
6. `a_plan()` - 52 edges
7. `GoogleMeetProvider` - 51 edges
8. `build_work_plan()` - 50 edges
9. `TokenSet` - 50 edges
10. `a_metadata()` - 48 edges

## Surprising Connections (you probably didn't know these)
- `test_network_failure_becomes_an_authentication_error()` --uses--> `ProviderAuthenticationError`  [INFERRED]
  test_jira_oauth.py → providers/base.py
- `test_refresh_without_a_token_fails_before_any_request()` --uses--> `ProviderAuthenticationError`  [INFERRED]
  test_jira_oauth.py → providers/base.py
- `test_other_403_is_reported_as_a_consent_problem()` --uses--> `ProviderConsentRequiredError`  [INFERRED]
  test_providers_teams.py → providers/base.py
- `test_network_error_is_reported_as_api_error()` --uses--> `ProviderAPIError`  [INFERRED]
  test_providers_google.py → providers/base.py
- `test_graph_network_error_is_reported()` --uses--> `ProviderAPIError`  [INFERRED]
  test_providers_teams.py → providers/base.py

## Import Cycles
- None detected.

## Communities (80 total, 9 thin omitted)

### Community 0 - "jira_planner.py"
Cohesion: 0.06
Nodes (56): _action_item_line(), assemble_plan(), _block(), _brd_section(), build_planning_prompt(), _coverage_notes(), _depth_by_type_name(), _Draft (+48 more)

### Community 1 - "test_main_integration.py"
Cohesion: 0.06
Nodes (52): Assumption, _flash(), _handle_oauth_callback(), _parse_action_items(), Helper to validate a list of requirements against source notes., Safely parse action item entries from dictionary or string formats., Validates the extracted data from Gemini against the original notes and maps it…, The registered OAuth redirect URI; must match the provider console entry. (+44 more)

### Community 2 - "test_jira_planner.py"
Cohesion: 0.10
Nodes (54): a_grouped_response(), note_matching(), one_item(), plan_from(), Jira planner tests: the AI grouping layer between an approved BRD and a work…, A well-formed model answer for ``a_complex_brd`` against ``FULL``. One…, The plan a model answer produces, with nothing else stubbed., A single-item response, for tests about one specific malformation. (+46 more)

### Community 3 - "test_jira_work_plan.py"
Cohesion: 0.13
Nodes (46): ActionItem, ProjectOverview, High-level project summary information., Requirement, build_work_plan(), Propose Jira issues for one project from one BRD. Creates nothing. One issue…, a_brd(), a_metadata() (+38 more)

### Community 4 - "main.py"
Cohesion: 0.07
Nodes (43): Stakeholder, _clear_jira_project_state(), _connected_tokens(), display_brd(), display_requirements(), _gemini_json(), _identity_label(), _load_transcript() (+35 more)

### Community 5 - "TokenSet"
Cohesion: 0.09
Nodes (37): ProviderAuthenticationError, ProviderTokenExpiredError, Raised when authentication with the provider fails., Raised on HTTP 401 from a provider API. Distinct from a generic authentication…, call_with_refresh(), Any, Expiry-aware OAuth token handling for provider sessions. Tokens live only in…, Exchange the refresh token for a new access token. (+29 more)

### Community 6 - "FakeResponse"
Cohesion: 0.08
Nodes (41): ProviderAPIError, Raised when a provider API call fails for a non-authorization reason., parametrize, 3LO tokens go to api.atlassian.com/ex/jira/{cloudid}; Atlassian states they…, The cloud id arrives from an API response and lands in a URL path., The envelope is unverified, so a plain array must not be a hard failure., Returning [] here would read as a permissions problem and send the user to a…, Re-querying sites invalidates everything below it, not just the selection. (+33 more)

### Community 7 - "test_lifecycle_workspace.py"
Cohesion: 0.07
Nodes (33): BRDData, lifecycle_from(), ProjectLifecycle, The project delivery lifecycle: which artifact exists, and how far it has been…, This stage's state, defaulting to not started., Set one stage's state. Raises on an unknown stage or status., Derive the current lifecycle from the artifacts this session actually holds.…, One stage's status, and the one line that explains why it holds that status. (+25 more)

### Community 8 - "test_transcript_ingestion.py"
Cohesion: 0.09
Nodes (36): BinaryIO, BytesIO, a_transcript(), The same meeting text arriving by each of the four supported routes., test_manual_and_upload_sources_still_leave_provider_fields_empty(), test_extract_text_from_empty_bytes(), test_extract_text_from_latin1_bytes(), test_extract_text_from_utf8_bytes() (+28 more)

### Community 9 - "test_jira_config.py"
Cohesion: 0.10
Nodes (34): _env(), get_client_id(), get_client_secret(), get_missing_configuration(), get_redirect_uri(), get_setup_instructions(), is_configured(), Jira Cloud integration configuration. This is the only module that knows which… (+26 more)

### Community 10 - "begin_handshake"
Cohesion: 0.12
Nodes (29): _b64d(), _b64e(), begin_handshake(), _derive_code_verifier(), OAuthHandshake, OAuthStateError, Exception, Stateless, signed OAuth handshake values (CSRF state + PKCE). Why the state is… (+21 more)

### Community 11 - "PROJECT_CONTEXT.md"
Cohesion: 0.06
Nodes (32): 1. Confirmed Requirement, 1. Project Overview, 2. Assumption, 2. Business Objectives, 3. Open Question, 3. Scope, 4. Explicitly Deferred or Out of Scope, 4. Stakeholders (+24 more)

### Community 12 - "test_providers_teams.py"
Cohesion: 0.07
Nodes (13): events_response(), FakeResponse, Microsoft Teams provider tests. Every Microsoft Graph call is monkeypatched: no…, An empty list would hide a consent problem; the user must see the real reason., SpeakerAttributionNotAllowed only affects VTT; the plain type must be retried., test_discovery_propagates_expired_token(), test_discovery_raises_the_permission_error_when_nothing_was_retrievable(), test_discovery_reports_no_online_meeting_events() (+5 more)

### Community 13 - "jira_processor.py"
Cohesion: 0.09
Nodes (26): JiraProjectMetadata, JiraWorkPlan, Non-secret Jira Cloud data models. Each model keeps only the fields this app…, What the selected project would require to create an issue in it. Read-only,…, Issue types whose every required field a work plan could supply., A proposal for what could be created in one Jira project. Held locally and…, Issues with no parent, in plan order., Issues naming ``plan_key`` as their parent, in plan order. (+18 more)

### Community 14 - "a_plan"
Cohesion: 0.14
Nodes (30): a_plan(), The plan as the session holds it after a render., Streamlit keys outlive the widget. A stale value under a deleted issue's key…, The heading is drawn before the expander that holds the editor, so the edit…, JIRA-006 stops at review. Creation is a later, explicit ticket., A generated plan to review. Defaults to the three-level shape., The Streamlit widget key ``main`` uses for one editor on one issue., Render the panel over an already-generated plan and capture what is shown. The… (+22 more)

### Community 15 - "test_jira_projects.py"
Cohesion: 0.13
Nodes (28): metadata_routes(), paged(), patch_routes(), JIRA-004 tests: Jira project and create-metadata discovery. Every HTTP call is…, Route GETs by URL. ``handler(url, params)`` returns the response. Needed…, A handler serving the create-metadata endpoints. An issue type absent from…, Serve ``pages`` in order, one per request, keyed by the requested startAt., Jira may answer with `total` and no `isLast`; that must still terminate. (+20 more)

### Community 16 - "TranscriptProvider"
Cohesion: 0.08
Nodes (16): ABC, Any, Check whether the necessary configuration/credentials are present., Return a list of missing configuration keys or items., Return detailed instructions for configuring this provider., Builds the OAuth 2.0 authorization URL for user consent., Exchanges an authorization code for an OAuth access token dictionary., Exchanges a refresh token for a fresh OAuth access token dictionary. (+8 more)

### Community 17 - "MSTeamsProvider"
Cohesion: 0.13
Nodes (13): MSTeamsProvider, Pull ``(codes, message)`` out of a Graph error body. Graph puts the actionable…, GET a Graph endpoint, translating HTTP failures into typed errors. 401 becomes…, Follow every ``@odata.nextLink`` page. Returns ``(items, truncated)``;…, Real provider adaptor for Microsoft Teams meeting transcripts via Microsoft…, Resolve a Teams join URL to its onlineMeeting resource. Graph v1.0 exposes no…, List the callTranscript resources attached to one online meeting., Discover Teams transcripts through the signed-in user's calendar. Graph v1.0… (+5 more)

### Community 18 - "GoogleMeetProvider"
Cohesion: 0.11
Nodes (14): GoogleMeetProvider, Detailed instructions for configuring Google Meet integration., Retrieves authenticated user profile information (email, name)., Extract a human-readable, secret-free error message from a response., GET a Google API endpoint, translating HTTP failures into typed errors. 401…, Real provider adaptor for Google Meet & Google Workspace meeting transcripts.…, Best-effort lookup of a space's human-usable meeting code. The Meet API does…, Best-effort read of the transcript resource for state and timing. (+6 more)

### Community 19 - "test_jira_oauth.py"
Cohesion: 0.10
Nodes (22): auth_params(), flash_text(), JIRA-002 tests: Jira Cloud OAuth 2.0 (3LO) connection. Every HTTP call is…, The shared connect button offers a PKCE challenge to every service. Atlassian…, A real JiraService in the callback path must fail with names, not values., Importing main already exercises this path; assert it stays exception-free., test_authorization_url_carries_every_documented_parameter(), test_authorization_url_omits_pkce_because_3lo_does_not_document_it() (+14 more)

### Community 20 - "_skey"
Cohesion: 0.18
Nodes (24): _disconnect(), Namespaced session-state key for one provider., Drop every trace of the provider session from this browser session., _skey(), test_disconnect_clears_every_project_and_metadata_key(), JIRA-003 tests: accessible Jira Cloud site discovery. Every HTTP call is…, Proves site discovery reuses call_with_refresh rather than its own retry., Render the site panel and capture what the user would see. Returns (messages,… (+16 more)

### Community 21 - "test_jira_work_plan_review.py"
Cohesion: 0.17
Nodes (23): Replace editable fields on one proposed issue. Unknown keys are ignored.…, Reasons this proposal is not ready. Empty means the selected issues are…, update_planned_issue(), validate_work_plan(), JIRA-006 tests: reviewing a generated Jira work plan before anything is…, NFR-1 has no subtask, so dropping it leaves nothing stranded., FR-1 carries a subtask. Dropping the parent while keeping the child would leave…, Deletion cascades so this should be unreachable through the UI, which is… (+15 more)

### Community 22 - "a_plan"
Cohesion: 0.15
Nodes (24): a_plan(), forbid_post(), This ticket's own version of ``forbid_write_requests``, for the paths that must…, Render the creation panel and capture what the user would see. ``clicked``…, Stored results are re-rendered with their links, so the trail stays available., Stored results short-circuit the panel, so the rerun that follows a creation --…, Validation runs immediately before the write, not only at review time., Blocked by validation, which reports the empty selection as a problem. (+16 more)

### Community 23 - "JiraService"
Cohesion: 0.09
Nodes (17): JiraService, Session-state namespace and OAuth state routing key., Configuration steps, with the scopes this ticket actually requests. The…, Build the Atlassian 3LO consent URL.…, The Jira Cloud OAuth connection used by the optional Jira section., Read-only except for the one thing the app creates. `read:jira-user` was added…, JIRA-007 adds issue creation, and nothing else. Everything the app never asked…, Jira must not appear in the transcript funnel. (+9 more)

### Community 24 - "test_providers_google.py"
Cohesion: 0.10
Nodes (14): _entry(), provider(), fixture, parametrize, Google Meet provider tests. Every Google API call is monkeypatched: no real…, An unnamed speaker must never be given an invented identity., The Google Docs artifact is the only remaining real source when entries are…, test_get_transcript_resolves_real_participant_names() (+6 more)

### Community 25 - "AI-Assisted Software Delivery Lifecycle"
Cohesion: 0.10
Nodes (19): 1. Manual Paste — working and used end to end, 2. Upload Transcript File (.txt) — working and used end to end, 3. Google Meet — implemented, unit-tested with mocked APIs, **not verified against a live Google account**, 4. Microsoft Teams — implemented, unit-tested with mocked APIs, **not verified against a live Microsoft 365 tenant**, AI-Assisted Software Delivery Lifecycle, Architecture and Data Flow, Core Principle: The Notes Are the Source of Truth, Credential and Token Handling (+11 more)

### Community 26 - "FakeResponse"
Cohesion: 0.15
Nodes (15): FakeResponse, Atlassian documents Content-Type: application/json on the token endpoint,…, Captures how the token endpoint was called, without ever logging it., RecordingPost, test_code_exchange_does_not_send_a_pkce_verifier(), test_code_exchange_posts_json_not_form_encoded(), test_code_exchange_returns_the_token_payload(), test_code_exchange_sends_the_documented_body_fields() (+7 more)

### Community 27 - "NormalizedTranscript"
Cohesion: 0.19
Nodes (14): NormalizedTranscript, Standardized representation of a meeting transcript from any source. Every…, Jira Cloud OAuth 2.0 (3LO) connection. Scope of this module…, ProviderConsentRequiredError, ProviderError, ProviderNotConfiguredError, Exception, Raised when an operation is attempted on an unconfigured provider. (+6 more)

### Community 28 - "Auto-BRD → AI Software Delivery Lifecycle"
Cohesion: 0.11
Nodes (18): 10. Reuse Existing Project Capabilities, 11. Remove Obsolete Functionality, 12. UI Direction, 13. AI + Human Governance, 14. Implementation Principles, 1. Product Vision, 2. Source-of-Truth Model, 3. Target End-to-End Workflow (+10 more)

### Community 29 - ".refresh_access_token"
Cohesion: 0.17
Nodes (8): Any, Returns missing configuration variable names., Generates the Microsoft Entra ID OAuth 2.0 authorization URL., Shared token endpoint call. Never logs or echoes the payload., Exchanges authorization code for Microsoft Graph access tokens., Refreshes an expired Microsoft Graph access token., Best-effort identity of the connected Microsoft 365 account., Checks if all required Azure / Teams credentials exist.

### Community 30 - "Router"
Cohesion: 0.14
Nodes (13): FakeResponse, Dispatches mocked requests.get calls by URL, recording every call., Router, test_discovery_lists_meet_transcripts_and_drive_documents(), test_discovery_marks_transcripts_that_are_not_ready(), test_discovery_propagates_expired_token(), test_discovery_reports_no_meetings_without_inventing_any(), test_discovery_reports_probe_limit_truncation() (+5 more)

### Community 31 - "Auto-BRD Generator — Product Requirements Document (PRD)"
Cohesion: 0.11
Nodes (17): Auto-BRD Generator — Product Requirements Document (PRD), Connect Jira, Core User Journey, Create Jira Issues, Current Capabilities, Discover Jira Resources, Future Scope, Generate Jira Work Plan (+9 more)

### Community 32 - "jira_tokens"
Cohesion: 0.16
Nodes (18): jira_tokens(), A session holding the scopes this ticket requests, unless a test narrows them., Render the project panel and capture what the user would see. Returns…, Proves project discovery reuses call_with_refresh rather than its own retry., A project list belongs to one site. Left in place it would offer projects that…, A session authorized before read:jira-work was requested can never see a…, Atlassian documents that a Jira account's own permissions still apply., render_projects_panel() (+10 more)

### Community 33 - ".get_project_metadata"
Cohesion: 0.16
Nodes (10): _clean(), Any, Build a project from one entry of a project-search response. Returns ``None``…, Build a field from one create-metadata field entry. ``fallback_id`` covers the…, Build an issue type from one create-metadata issue-type entry. Returns ``None``…, Trimmed string form of an API value, with ``None`` becoming ``""``., Address a REST path on one Jira site. 3LO tokens are used against…, Collect every page of a Jira ``startAt``/``maxResults`` collection. Returns… (+2 more)

### Community 34 - "a_complex_brd"
Cohesion: 0.16
Nodes (17): action_item_index(), Map of planner-minted action-item id to the BRD action item it names. Ordered…, a_complex_brd(), a_model(), A stand-in for the model: returns fixed text, and records the prompt it was…, ``conftest`` blanks ``main.CLIENT``, so this is the no-key path end to end., A BRD with several distinct areas of work, which is where a 1:1 mapping fails.…, The autouse fixtures fail the test on any HTTP verb, including ``get``. (+9 more)

### Community 35 - "_render_jira_work_plan_panel"
Cohesion: 0.12
Nodes (17): _clear_jira_plan_review_widgets(), _criteria_from_text(), _criteria_text(), _linked_action_items(), _persist_work_plan(), _planner_generate(), The model call the Jira planner should use, or ``None`` when none is…, Step 4: propose Jira issues from the reviewed BRD. Creates nothing. The plan… (+9 more)

### Community 36 - "patch_post"
Cohesion: 0.16
Nodes (14): a_payload(), create(), patch_post(), Records each POST and answers from a queue of responses., One create call, so the error tests do not each repeat the argument list., Method, URL, headers and body against Atlassian's 3LO contract: the site-scoped…, The safety property of this ticket. ``call_with_refresh`` reacts to…, The request may have reached Jira, so reporting "it failed" would be a guess. (+6 more)

### Community 37 - "parse_planner_response"
Cohesion: 0.15
Nodes (15): _first_json_span(), _item_rows(), _json_payload(), parse_planner_response(), PlannerResponseError, Exception, The model's answer could not be read as a work plan. Carries a message safe to…, The first balanced ``{...}`` or ``[...]`` in ``text``, or ``None``. String… (+7 more)

### Community 38 - ".refresh_access_token"
Cohesion: 0.23
Nodes (7): Any, Generates the Google OAuth 2.0 user consent URL., Shared token endpoint call. Never logs or echoes the payload., Exchanges authorization code for access and refresh tokens., Refreshes an expired access token using the refresh token., Checks if required OAuth credentials are set in environment., Returns missing configuration variable names.

### Community 39 - "test_jira_creation.py"
Cohesion: 0.18
Nodes (13): issue_browse_url(), The page a created issue can be opened at on its own Jira site.…, paragraphs(), JIRA-007 tests: creating the approved Jira issues. This is the first ticket…, v3 takes rich text as ADF, not as a string., A Jira create screen has no acceptance-criteria field of its own, and writing…, The scope set itself is asserted in ``test_jira_oauth``. What matters here is…, ``jira_processor`` builds the payload and the order but must not be able to… (+5 more)

### Community 40 - "Auto-BRD Generator — Feature Ticket List"
Cohesion: 0.15
Nodes (12): Auto-BRD Generator — Feature Ticket List, Development Rules, Future: JIRA-010 — Requirement Change Detection, JIRA-001 — Jira Integration Foundation, JIRA-002 — Jira OAuth Connection, JIRA-003 — Accessible Site Discovery, JIRA-004 — Project and Metadata Discovery, JIRA-005 — BRD to Jira Work Plan (+4 more)

### Community 41 - "JiraSite"
Cohesion: 0.21
Nodes (10): JiraSite, One Jira Cloud site the connected Atlassian account granted access to. Built…, Label for a picker. Contains no token material., Build a site from one accessible-resources entry. Returns ``None`` when the…, The Jira Cloud sites this authorization can reach. Wraps ``GET…, parametrize, test_a_selected_site_cannot_be_mutated_in_place(), test_display_label_falls_back_without_inventing_anything() (+2 more)

### Community 42 - "_create_selected_issues"
Cohesion: 0.23
Nodes (13): _create_selected_issues(), Create the selected issues, parents first, and report each outcome. Stops at…, created_ok(), A create-issue success body: Jira returns id, key and self., The plan holds plan-local keys. Sending one as ``parent`` would name an issue…, Continuing past a failure would create children whose parent is missing. The…, The mapping is recorded as the run happens, not looked up in the plan…, test_a_child_is_sent_the_key_its_parent_was_actually_created_as() (+5 more)

### Community 43 - "._transcript_from_meet_entries"
Cohesion: 0.19
Nodes (6): Walk every page of a Google list endpoint. Returns ``(items, truncated)``.…, Find every Meet transcript this account can actually reach. Two real sources…, Lists available meeting transcripts from Google Meet and Google Drive., Retrieves and normalizes a transcript from Google Meet API or Google Drive., Resolve a conference record to its usable transcript, then fetch it., Build a transcript from Meet API transcript entries, fully paginated.

### Community 44 - "Auto-BRD Generator — Technical Architecture"
Cohesion: 0.15
Nodes (12): Architecture Overview, Authentication and Session State, Auto-BRD Generator — Technical Architecture, Constraints, Data and Side-Effect Boundary, Error Handling, High-Level Flow, Jira Flow (+4 more)

### Community 45 - "Auto-BRD Generator — Frontend Specification"
Cohesion: 0.17
Nodes (11): Auto-BRD Generator — Frontend Specification, Confirmation, Design Principles, Errors, Existing Main Flow, Generate Work Plan, Jira Entry Point, Review Interface (+3 more)

### Community 46 - "generate_work_plan"
Cohesion: 0.17
Nodes (12): generate_work_plan(), The same plan with ``note`` first, so the reason leads the list a reviewer…, Propose Jira issues for one project from one approved BRD. Creates nothing.…, _with_note(), plannable_levels(), Every rung of this project's hierarchy a plan may use, deepest container first.…, _imported_modules(), Only the exception type is reported. A client's message can carry request… (+4 more)

### Community 47 - "Auto-BRD Generator — Security & Access"
Cohesion: 0.17
Nodes (11): Auto-BRD Generator — Security & Access, Dependency Security, Google Meet, Jira, Logging, Microsoft Teams, OAuth State, Principles (+3 more)

### Community 48 - "CreatedIssue"
Cohesion: 0.18
Nodes (11): CreatedIssue, One issue creation that was attempted in Jira, and how it turned out.…, What a creation run actually did, successes and failures kept apart. Rendered…, One creation outcome, carrying the plan item's own traceability with it., Step 5: create the selected issues, after an explicit confirmation. Two…, _render_created_results(), _render_jira_creation_panel(), _result_for() (+3 more)

### Community 49 - "PlannedIssue"
Cohesion: 0.18
Nodes (9): PlannedIssue, One issue a work plan proposes. Nothing here exists in Jira. ``plan_key`` is…, Every BRD requirement id this issue traces back to, however it was planned., Whether this issue names something in the BRD it came from. An issue that…, issue_description_text(), The description text this app sends to Jira for one proposed issue. Acceptance…, With no reported flag to match against, guessing one would invent hierarchy., test_a_type_the_project_never_reported_makes_nothing_compatible() (+1 more)

### Community 50 - "a_type"
Cohesion: 0.22
Nodes (11): choose_issue_types(), Pick the issue types a plan may use, from what Jira reported for this project.…, a_type(), The container is chosen because Jira put it at a higher level, not because it…, Nothing can be ordered against an unreported level, so such a type carries a…, Several types commonly share one level. Choosing between them on any basis…, A discovered issue type. Defaults to one that passed the required-field check., test_a_subtask_issue_is_only_offered_subtask_types() (+3 more)

### Community 51 - "Any"
Cohesion: 0.22
Nodes (6): Any, Which of the scopes this app now requests are absent from a live session. A…, Call Atlassian's token endpoint with a JSON body. Uses ``json=`` rather than…, Exchange an authorization code for tokens. ``code_verifier`` is accepted for…, Trade a refresh token for a fresh access token. Atlassian rotates refresh…, Best-effort Atlassian account identity, so the UI can show who is connected.…

### Community 52 - "JiraIssueType"
Cohesion: 0.25
Nodes (5): JiraIssueType, One issue type the connected account could create in the selected project.…, Required fields a generated work plan could not supply., ``"ok"``, ``"blocked"`` or ``"unknown"``. Three states rather than two on…, What Jira said about this type's place in the hierarchy, verbatim in substance.…

### Community 53 - "JiraProject"
Cohesion: 0.25
Nodes (6): JiraProject, One Jira project the connected account can see on the selected site. Only what…, Label for a picker. Contains no token material., The value for a ``{projectIdOrKey}`` path segment. The numeric id is preferred:…, test_a_plan_built_for_another_project_is_reported(), test_metadata_read_for_another_project_is_reported()

### Community 54 - "issue_creation_payload"
Cohesion: 0.25
Nodes (8): _adf(), issue_creation_payload(), Plain text as an Atlassian Document Format doc. VERIFICATION NOTE. Jira Cloud…, The request body for creating one proposed issue. Sends nothing itself.…, parametrize, Order is derived, not trusted: a review edit or a delete can leave a child…, test_parents_come_before_the_children_that_name_them(), test_the_project_is_addressed_by_id_or_key_as_given()

### Community 55 - "creation_order"
Cohesion: 0.25
Nodes (8): creation_order(), The selected issues, parents before the children that name them. Jira cannot be…, a_cycle(), Its child would otherwise name a parent that is not being created., The plan with two issues made each other's parent, so neither can be ordered., The same plan with one issue changed, still frozen., test_a_deselected_issue_is_not_created_and_takes_its_children_with_it(), with_issue()

### Community 56 - "delete_planned_issue"
Cohesion: 0.25
Nodes (8): delete_planned_issue(), Remove one proposed issue and every descendant. Cascade is required: a leftover…, A leftover child would name a parent that no longer exists, which is an…, test_deleting_a_parent_takes_its_descendants_with_it(), test_deleting_an_issue_removes_it(), test_deleting_an_issue_that_is_not_in_the_plan_changes_nothing(), test_deleting_the_container_empties_the_whole_plan(), test_no_deletion_ever_leaves_an_issue_naming_a_missing_parent()

### Community 57 - ".create_issue"
Cohesion: 0.25
Nodes (4): Human-readable reason from a failed Atlassian response. Atlassian uses…, GET an Atlassian endpoint, mapping HTTP status onto the shared provider error…, Decode a successful Atlassian response, or say which call broke. ``what`` names…, POST one issue to Jira. The only call in this app that changes anything.…

### Community 58 - "parametrize"
Cohesion: 0.25
Nodes (8): parametrize, Plan key, parent and type identity are hierarchy, not content. Letting the…, The strongest form of "creates nothing": every HTTP verb is a test failure., test_an_incompatible_type_is_refused(), test_an_unsupported_issue_type_is_reported(), test_every_path_that_invalidates_the_plan_also_clears_editor_state(), test_no_review_action_sends_a_request(), test_structural_fields_are_not_editable_through_the_field_editor()

### Community 59 - "PlanLevel"
Cohesion: 0.33
Nodes (3): PlanLevel, One rung of the selected project's own hierarchy that a plan may use. ``depth``…, The type at this rung whose Jira name matches ``name``, or ``None``. Exact…

### Community 60 - "TranscriptUnavailableError"
Cohesion: 0.33
Nodes (6): Raised when the provider is reachable and authorized, but there is simply no…, TranscriptUnavailableError, test_conference_record_without_transcript_explains_transcription_requirement(), test_empty_transcript_content_is_reported(), test_expired_meeting_transcript_lookup_is_reported(), test_tenant_disabled_graph_transcript_access_is_reported_as_unavailable()

### Community 61 - "clean_session"
Cohesion: 0.33
Nodes (6): clean_session(), configured(), forbid_write_requests(), fixture, A fresh browser session and no Jira credentials unless a test asks., Site discovery is read-only. Any HTTP verb that could change something in Jira…

### Community 62 - "no_live_model_calls"
Cohesion: 0.40
Nodes (4): no_live_model_calls(), fixture, Test-wide safety net: no test may make a live language-model call. ``main``…, Make ``main``'s model client unavailable for the duration of every test.

### Community 65 - "clean_env"
Cohesion: 0.40
Nodes (5): clean_env(), configured(), jira_callback(), fixture, No Jira credentials unless a test asks for them.

### Community 66 - "JiraField"
Cohesion: 0.50
Nodes (3): JiraField, One field Jira reported on an issue type's create screen. ``required`` is…, Whether a generated work plan could supply this field. True when the field is…

### Community 67 - "test_all_four_sources_unified_pipeline"
Cohesion: 0.50
Nodes (4): convert_brd_to_markdown(), Converts the BRDData object into a Markdown string., Verifies that all 4 sources (Manual, TXT Upload, Google Meet, MS Teams) produce…, test_all_four_sources_unified_pipeline()

### Community 68 - "generate_brd_from_notes"
Cohesion: 0.50
Nodes (4): generate_brd_from_notes(), generate_brd_from_transcript(), Sends meeting notes to the Gemini API and returns a structured BRDData object., Passes a normalized transcript through the BRD generation and evidence…

### Community 69 - "test_ms_teams_list_transcripts"
Cohesion: 0.33
Nodes (3): Detailed instructions for configuring Microsoft Teams integration., Discovery uses the supported delegated Graph flow: /me/events ->…, test_ms_teams_list_transcripts()

### Community 71 - "_requirement_description"
Cohesion: 0.67
Nodes (3): _quote(), Render text as a Markdown block quote, preserving its own line breaks., _requirement_description()

### Community 73 - "no_http_at_all"
Cohesion: 0.67
Nodes (3): no_http_at_all(), fixture, Planning reads no Jira endpoint. ``forbid_write_requests`` covers the rest.

### Community 74 - "test_the_plan_is_cleared_by_every_path_that_invalidates_it"
Cohesion: 0.67
Nodes (3): parametrize, Disconnecting and changing the project both have to drop the plan. Token…, test_the_plan_is_cleared_by_every_path_that_invalidates_it()

## Knowledge Gaps
- **118 isolated node(s):** `graphify`, `Workflow: graphify`, `1. Product Vision`, `2. Source-of-Truth Model`, `Stage 1 — Discovery → BRD` (+113 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **9 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `GoogleMeetProvider` connect `GoogleMeetProvider` to `test_all_four_sources_unified_pipeline`, `main.py`, `TokenSet`, `FakeResponse`, `.refresh_access_token`, `test_transcript_ingestion.py`, `._transcript_from_meet_entries`, `TranscriptProvider`, `test_providers_google.py`, `NormalizedTranscript`, `TranscriptUnavailableError`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Why does `TokenSet` connect `TokenSet` to `.is_expired`, `test_main_integration.py`, `main.py`, `begin_handshake`, `test_providers_teams.py`, `test_jira_oauth.py`, `_skey`, `test_providers_google.py`?**
  _High betweenness centrality (0.051) - this node is a cross-community bridge._
- **Why does `MSTeamsProvider` connect `MSTeamsProvider` to `test_all_four_sources_unified_pipeline`, `main.py`, `TokenSet`, `FakeResponse`, `test_ms_teams_list_transcripts`, `test_transcript_ingestion.py`, `test_providers_teams.py`, `provider`, `TranscriptProvider`, `NormalizedTranscript`, `TranscriptUnavailableError`, `.refresh_access_token`, `.parse_vtt`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **Are the 29 inferred relationships involving `JiraService` (e.g. with `JiraField` and `JiraIssueType`) actually correct?**
  _`JiraService` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `FakeResponse` (e.g. with `test_an_unreadable_body_is_an_api_error_not_an_empty_list()` and `test_http_failures_map_onto_the_shared_error_hierarchy()`) actually correct?**
  _`FakeResponse` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 10 inferred relationships involving `MSTeamsProvider` (e.g. with `NormalizedTranscript` and `ProviderAPIError`) actually correct?**
  _`MSTeamsProvider` has 10 INFERRED edges - model-reasoned connections that need verification._
- **What connects `graphify`, `Workflow: graphify`, `1. Product Vision` to the rest of the system?**
  _118 weakly-connected nodes found - possible documentation gaps or missing edges._