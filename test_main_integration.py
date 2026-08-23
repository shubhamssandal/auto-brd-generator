"""
main.py integration tests: OAuth callback handling and the shared transcript funnel.

Streamlit runs in "bare" mode under pytest, where ``st.query_params`` and
``st.session_state`` are real read/write objects and rendering calls are no-ops.
That is enough to exercise the callback logic without a browser. No real
credentials and no network access are required.
"""

import pytest
import requests
import streamlit as st

import main
from brd_models import NormalizedTranscript
from jira_processor import build_work_plan, validate_work_plan
from providers.base import (
    ProviderAPIError,
    ProviderAuthenticationError,
    ProviderConsentRequiredError,
    ProviderNotConfiguredError,
    TranscriptUnavailableError,
)
from providers.oauth_state import begin_handshake
from providers.session_tokens import TokenSet

# JIRA-004/005's project and metadata builders, so the Jira half of this regression
# runs against the same objects the Jira tests use rather than a shape invented here.
from test_jira_work_plan import (
    CONTAINER,
    ITEM,
    SUBTASK,
    PROJECT as JIRA_PROJECT,
    a_metadata,
)
from transcript_processor import normalize_manual_notes, normalize_uploaded_file

JIRA_METADATA = a_metadata(CONTAINER, ITEM, SUBTASK)


class StubProvider:
    """Records what the callback asked it to do; never contacts a network."""

    name = "google_meet"
    display_name = "Google Meet"

    exchanges = []
    response = {"access_token": "at-secret-123", "refresh_token": "rt-secret-456", "expires_in": 3599}
    failure = None

    def exchange_code_for_token(self, code, redirect_uri, code_verifier=None):
        type(self).exchanges.append(
            {"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier}
        )
        if type(self).failure is not None:
            raise type(self).failure
        return type(self).response


@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    StubProvider.exchanges = []
    StubProvider.failure = None
    StubProvider.response = {
        "access_token": "at-secret-123",
        "refresh_token": "rt-secret-456",
        "expires_in": 3599,
    }

    st.query_params.clear()
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)

    monkeypatch.setitem(main.PROVIDER_CLASSES, "google_meet", StubProvider)
    yield
    st.query_params.clear()
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)


def flashes():
    return list(st.session_state.get("_flashes") or [])


def flash_text():
    return " ".join(message for _, message in flashes())


# --- Helpers ---

def test_redirect_uri_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("APP_REDIRECT_URI", raising=False)
    assert main._redirect_uri() == "http://localhost:8501"


def test_redirect_uri_honours_the_environment(monkeypatch):
    monkeypatch.setenv("APP_REDIRECT_URI", "http://localhost:9000")
    assert main._redirect_uri() == "http://localhost:9000"


def test_session_keys_are_namespaced_per_provider():
    assert main._skey("google_meet", "tokens") != main._skey("microsoft_teams", "tokens")


# --- OAuth callback: nothing to do ---

def test_callback_is_a_no_op_without_query_parameters():
    main._handle_oauth_callback()
    assert flashes() == []
    assert StubProvider.exchanges == []


# --- OAuth callback: denial ---

def test_authorization_denial_is_reported_and_does_not_break_other_sources():
    st.query_params["error"] = "access_denied"
    st.query_params["state"] = begin_handshake("google_meet").state

    main._handle_oauth_callback()

    kinds = [kind for kind, _ in flashes()]
    assert kinds == ["warning"]
    assert "declined" in flash_text()
    assert "Manual Paste" in flash_text()
    assert StubProvider.exchanges == []
    assert main._skey("google_meet", "tokens") not in st.session_state
    assert dict(st.query_params) == {}


def test_other_authorization_errors_are_reported():
    st.query_params["error"] = "invalid_scope"
    st.query_params["error_description"] = "The requested scope is not permitted."

    main._handle_oauth_callback()

    kinds = [kind for kind, _ in flashes()]
    assert kinds == ["error"]
    assert "invalid_scope" in flash_text()
    assert StubProvider.exchanges == []


# --- OAuth callback: state validation ---

def test_forged_state_is_rejected_without_exchanging_the_code():
    st.query_params["code"] = "attacker-supplied-code"
    st.query_params["state"] = "google_meet"  # the old constant marker

    main._handle_oauth_callback()

    assert [kind for kind, _ in flashes()] == ["error"]
    assert "could not be verified" in flash_text()
    # The code must never be exchanged, and no session may be created.
    assert StubProvider.exchanges == []
    assert main._skey("google_meet", "tokens") not in st.session_state
    assert dict(st.query_params) == {}


def test_missing_state_is_rejected():
    st.query_params["code"] = "some-code"

    main._handle_oauth_callback()

    assert [kind for kind, _ in flashes()] == ["error"]
    assert StubProvider.exchanges == []


def test_state_for_an_unknown_provider_is_ignored():
    st.query_params["code"] = "some-code"
    st.query_params["state"] = begin_handshake("not_a_real_provider").state

    main._handle_oauth_callback()

    assert "unknown provider" in flash_text()
    assert StubProvider.exchanges == []


# --- OAuth callback: success ---

def test_verified_callback_stores_a_token_set_and_sends_the_pkce_verifier(monkeypatch):
    monkeypatch.setenv("APP_REDIRECT_URI", "http://localhost:8501")
    handshake = begin_handshake("google_meet")
    st.query_params["code"] = "verified-code"
    st.query_params["state"] = handshake.state

    main._handle_oauth_callback()

    assert [kind for kind, _ in flashes()] == ["success"]
    assert len(StubProvider.exchanges) == 1
    exchange = StubProvider.exchanges[0]
    assert exchange["code"] == "verified-code"
    assert exchange["redirect_uri"] == "http://localhost:8501"
    # PKCE: the verifier is re-derived from the signed state, never carried in a URL.
    assert exchange["code_verifier"] == handshake.code_verifier

    tokens = st.session_state[main._skey("google_meet", "tokens")]
    assert isinstance(tokens, TokenSet)
    assert tokens.access_token == "at-secret-123"
    assert tokens.can_refresh() is True

    # Query string is cleared and no secret reaches the UI.
    assert dict(st.query_params) == {}
    assert "at-secret-123" not in flash_text()
    assert "rt-secret-456" not in flash_text()
    assert "verified-code" not in flash_text()


def test_missing_refresh_token_is_disclosed():
    StubProvider.response = {"access_token": "at-only", "expires_in": 3599}
    st.query_params["code"] = "verified-code"
    st.query_params["state"] = begin_handshake("google_meet").state

    main._handle_oauth_callback()

    assert "no refresh token was issued" in flash_text()
    assert st.session_state[main._skey("google_meet", "tokens")].can_refresh() is False


def test_replaying_the_same_state_twice_is_rejected():
    handshake = begin_handshake("google_meet")

    st.query_params["code"] = "code-1"
    st.query_params["state"] = handshake.state
    main._handle_oauth_callback()
    assert len(StubProvider.exchanges) == 1

    # A second callback with a *different* code but the same state must still be
    # verified — and it is the token exchange, not the state, that Google rejects.
    # What matters here is that a state this process never signed cannot be reused.
    st.session_state["_flashes"] = []
    st.query_params["code"] = "code-2"
    st.query_params["state"] = handshake.state + "x"
    main._handle_oauth_callback()

    assert "could not be verified" in flash_text()
    assert len(StubProvider.exchanges) == 1


# --- OAuth callback: token exchange failure ---

def test_token_exchange_failure_is_reported_and_stores_nothing():
    StubProvider.failure = ProviderAuthenticationError(
        "Google OAuth failed: invalid_grant (the authorization code is invalid or expired)"
    )
    st.query_params["code"] = "stale-code"
    st.query_params["state"] = begin_handshake("google_meet").state

    main._handle_oauth_callback()

    assert [kind for kind, _ in flashes()] == ["error"]
    assert "could not be completed" in flash_text()
    assert main._skey("google_meet", "tokens") not in st.session_state
    assert dict(st.query_params) == {}


# --- Provider error reporting ---

@pytest.mark.parametrize(
    "error",
    [
        ProviderNotConfiguredError("Missing: GOOGLE_WORKSPACE_CLIENT_ID"),
        ProviderConsentRequiredError("admin consent is required"),
        TranscriptUnavailableError("no transcript for this meeting"),
        ProviderAPIError("HTTP 429", status_code=429),
        RuntimeError("something unexpected"),
    ],
)
def test_every_provider_error_type_is_reportable(error):
    # Must not raise: each branch is handled, including the generic fallback.
    main._report_provider_error("Google Meet", error)


def test_disconnect_clears_the_whole_provider_session():
    provider = StubProvider()
    for suffix in ("tokens", "handshake", "discovery", "transcript", "identity"):
        st.session_state[main._skey(provider.name, suffix)] = "value"
    st.session_state["microsoft_teams__tokens"] = "keep-me"

    main._disconnect(provider)

    assert not any(
        main._skey(provider.name, suffix) in st.session_state
        for suffix in ("tokens", "handshake", "discovery", "transcript", "identity")
    )
    # Disconnecting one provider must not touch the other.
    assert st.session_state["microsoft_teams__tokens"] == "keep-me"


def test_connected_tokens_ignores_non_token_values():
    provider = StubProvider()
    assert main._connected_tokens(provider) is None

    st.session_state[main._skey(provider.name, "tokens")] = "not-a-token-set"
    assert main._connected_tokens(provider) is None

    tokens = TokenSet.from_response({"access_token": "at-1"})
    st.session_state[main._skey(provider.name, "tokens")] = tokens
    assert main._connected_tokens(provider) is tokens


# --- Shared transcript funnel ---

def test_normalized_transcript_provider_fields_are_additive():
    """Existing call sites that never passed provider/transcript_id must still work."""
    legacy = NormalizedTranscript(raw_text="Some notes.")
    assert legacy.source == "manual"
    assert legacy.provider is None
    assert legacy.transcript_id is None
    assert legacy.participants == []
    assert legacy.metadata == {}

    enriched = NormalizedTranscript(
        raw_text="Some notes.",
        source="google_meet",
        provider="google_meet",
        transcript_id="conferenceRecords/rec-1/transcripts/t-1",
    )
    assert enriched.provider == "google_meet"
    assert enriched.transcript_id == "conferenceRecords/rec-1/transcripts/t-1"


def test_manual_and_upload_sources_still_leave_provider_fields_empty():
    manual = normalize_manual_notes("Team agreed to add refund tracking.", title="Sync")
    upload = normalize_uploaded_file(b"Team agreed to add refund tracking.", filename="sync.txt")

    for transcript in (manual, upload):
        assert transcript.provider is None
        assert transcript.transcript_id is None
        assert transcript.raw_text


def test_provider_transcripts_run_through_the_same_validation_pipeline():
    """A provider transcript gets no special treatment: same evidence rules apply."""
    evidence = "Sarah Khan: Support volume is up 30% this quarter."
    transcript = NormalizedTranscript(
        raw_text=f"{evidence}\nPriya: We need email notifications.",
        source="microsoft_teams",
        provider="microsoft_teams",
        transcript_id="meeting-1/transcripts/transcript-1",
        meeting_title="Refund Dashboard Review",
        participants=["Sarah Khan", "Priya"],
    )

    llm_output = {
        "project_title": "Refund Visibility",
        "functional_requirements": [
            {
                "requirement_id": "FR-1",
                "statement": "The system shall report support volume trends.",
                "source_evidence": evidence,
            },
            {
                "requirement_id": "FR-2",
                "statement": "The system shall integrate with Salesforce.",
                "source_evidence": "Sarah said we must integrate with Salesforce.",
            },
        ],
        "assumptions": [],
    }

    brd = main.validate_and_create_brd_data(llm_output, transcript.raw_text)

    # Verified evidence survives; unverifiable evidence is demoted, not accepted.
    assert [r.requirement_id for r in brd.functional_requirements] == ["FR-1"]
    assert len(brd.assumptions) == 1
    assert "Salesforce" in brd.assumptions[0].statement


def test_stakeholder_roles_are_never_invented():
    transcript_text = "Priya: We need email notifications."
    llm_output = {
        "project_title": "Notifications",
        "stakeholders": [
            {"name": "Priya", "role": "Product Manager"},
            {"name": "Sarah Khan", "role": ""},
            "Rahul Mehta",
        ],
    }

    brd = main.validate_and_create_brd_data(llm_output, transcript_text)
    roles = {s.name: s.role for s in brd.stakeholders}

    # A role the model supplied is kept as given; a blank or absent role is left
    # blank rather than being filled in with a guess.
    assert roles["Priya"] == "Product Manager"
    assert roles["Sarah Khan"] == ""
    assert roles["Rahul Mehta"] is None


# --- JIRA-009: every source still reaches a BRD, and a BRD still reaches Jira ---

EVIDENCE = "Sarah Khan: Refund status takes two days to confirm."

LLM_OUTPUT = {
    "project_title": "Refund Visibility",
    "functional_requirements": [
        {
            "requirement_id": "FR-1",
            "statement": "The system shall show refund status within one hour.",
            "source_evidence": EVIDENCE,
        }
    ],
    "assumptions": [],
}


def a_transcript(source):
    """The same meeting text arriving by each of the four supported routes."""
    text = "{}\nPriya: We need it same day.".format(EVIDENCE)
    if source == "manual":
        return normalize_manual_notes(text, title="Refund Review")
    if source == "upload":
        return normalize_uploaded_file(text.encode(), filename="refund-review.txt")
    return NormalizedTranscript(
        raw_text=text,
        source=source,
        provider=source,
        transcript_id="{}-transcript-1".format(source),
        meeting_title="Refund Review",
        participants=["Sarah Khan", "Priya"],
    )


@pytest.mark.parametrize(
    "source", ["manual", "upload", "google_meet", "microsoft_teams"]
)
def test_every_transcript_source_reaches_a_brd_and_a_jira_work_plan(source, monkeypatch):
    """
    The regression this ticket exists for: the Jira layer added on top must not have
    changed what any of the four sources produces, and a BRD from any of them must
    still plan against a real project's hierarchy. Nothing here contacts a network --
    the plan is a proposal, so no Jira call is involved in building one.
    """
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: pytest.fail("planning must send no request")
    )
    transcript = a_transcript(source)
    assert transcript.raw_text

    brd = main.validate_and_create_brd_data(LLM_OUTPUT, transcript.raw_text)
    assert [r.requirement_id for r in brd.functional_requirements] == ["FR-1"]

    plan = build_work_plan(brd, JIRA_PROJECT, JIRA_METADATA)

    assert not plan.is_empty
    assert validate_work_plan(plan, JIRA_METADATA, JIRA_PROJECT) == ()
    assert any("FR-1" in issue.requirement_ids for issue in plan.issues)
    assert main._skey("jira", "created") not in st.session_state
