"""
JIRA-002 tests: Jira Cloud OAuth 2.0 (3LO) connection.

Every HTTP call is monkeypatched, so no Atlassian app, no real credentials and no
network access are required. Credential values used here are obvious fakes.

The assertions encode the parts of Atlassian's documented 3LO contract that
differ from the Google and Microsoft flows already in this repo -- a JSON token
body rather than a form-encoded one, `audience=api.atlassian.com`, and rotating
refresh tokens -- because those are exactly the places a copied implementation
would silently be wrong.
"""

import urllib.parse

import pytest
import streamlit as st

import jira_config
import jira_service
import main
from jira_service import JiraService
from providers.base import (
    ProviderAuthenticationError,
    ProviderNotConfiguredError,
)
from providers.oauth_state import begin_handshake
from providers.session_tokens import TokenSet

CLIENT_ID = "jira-client-id-fake"
CLIENT_SECRET = "jira-client-secret-fake"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No Jira credentials unless a test asks for them."""
    for name in (
        jira_config.CLIENT_ID_VAR,
        jira_config.CLIENT_SECRET_VAR,
        jira_config.REDIRECT_URI_VAR,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(jira_config.CLIENT_ID_VAR, CLIENT_ID)
    monkeypatch.setenv(jira_config.CLIENT_SECRET_VAR, CLIENT_SECRET)
    return JiraService()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class RecordingPost:
    """Captures how the token endpoint was called, without ever logging it."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        return self.response

    @property
    def last(self):
        return self.calls[-1]


def auth_params(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


# --- Authorization request ------------------------------------------------

def test_authorization_url_uses_the_documented_atlassian_endpoint(configured):
    url = configured.get_authorization_url("http://localhost:8501")
    assert url.startswith("https://auth.atlassian.com/authorize?")


def test_authorization_url_carries_every_documented_parameter(configured):
    handshake = begin_handshake("jira")
    params = auth_params(
        configured.get_authorization_url("http://localhost:8501", state=handshake.state)
    )

    assert params["audience"] == ["api.atlassian.com"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == ["http://localhost:8501"]
    assert params["response_type"] == ["code"]
    assert params["state"] == [handshake.state]
    # offline_access only yields a refresh token when consent is displayed.
    assert params["prompt"] == ["consent"]


def test_authorization_url_space_separates_scopes(configured):
    params = auth_params(configured.get_authorization_url("http://localhost:8501"))
    assert params["scope"] == [" ".join(JiraService.SCOPES)]
    assert params["scope"][0].split(" ") == list(JiraService.SCOPES)


def test_authorization_url_omits_pkce_because_3lo_does_not_document_it(configured):
    """
    The shared connect button offers a PKCE challenge to every service. Atlassian
    documents only audience/client_id/scope/redirect_uri/state/response_type/
    prompt, so the challenge must be accepted and dropped rather than sent as an
    undocumented parameter.
    """
    handshake = begin_handshake("jira")
    url = configured.get_authorization_url(
        "http://localhost:8501",
        state=handshake.state,
        code_challenge=handshake.code_challenge,
        code_challenge_method=handshake.code_challenge_method,
    )

    assert "code_challenge" not in url
    assert "code_challenge_method" not in url
    assert handshake.code_challenge not in url
    # The signed state is still present: it, not PKCE, is what Atlassian's own
    # documentation credits with preventing a hijacked authorization flow.
    assert auth_params(url)["state"] == [handshake.state]


def test_authorization_url_never_contains_the_client_secret(configured):
    url = configured.get_authorization_url("http://localhost:8501")
    assert CLIENT_SECRET not in url


def test_authorization_url_requires_configuration():
    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        JiraService().get_authorization_url("http://localhost:8501")

    message = str(excinfo.value)
    assert "JIRA_CLIENT_ID" in message
    assert "JIRA_CLIENT_SECRET" in message


def test_requested_scopes_are_least_privilege():
    """
    JIRA-002 connects an account; it does not read or write Jira. Asking for a
    permission the app cannot yet exercise would misrepresent the consent screen.
    """
    assert set(JiraService.SCOPES) == {"read:me", "offline_access"}
    assert not [s for s in JiraService.SCOPES if s.startswith("write:")]
    assert not [s for s in JiraService.SCOPES if s.startswith("manage:")]


def test_setup_instructions_name_the_required_scopes_but_no_values(configured):
    instructions = configured.get_setup_instructions()

    assert "JIRA_CLIENT_ID" in instructions
    for scope in JiraService.SCOPES:
        assert scope in instructions
    assert CLIENT_ID not in instructions
    assert CLIENT_SECRET not in instructions


# --- Token exchange -------------------------------------------------------

def test_code_exchange_posts_json_not_form_encoded(configured, monkeypatch):
    """
    Atlassian documents Content-Type: application/json on the token endpoint,
    unlike Google's and Microsoft's form-encoded endpoints. Passing `data=`
    instead of `json=` is the copy-paste failure this test exists to catch.
    """
    post = RecordingPost(FakeResponse(payload={"access_token": "at-fake", "expires_in": 3600}))
    monkeypatch.setattr(jira_service.requests, "post", post)

    configured.exchange_code_for_token("auth-code-fake", "http://localhost:8501")

    assert post.last["url"] == "https://auth.atlassian.com/oauth/token"
    assert "json" in post.last["kwargs"]
    assert "data" not in post.last["kwargs"]


def test_code_exchange_sends_the_documented_body_fields(configured, monkeypatch):
    post = RecordingPost(FakeResponse(payload={"access_token": "at-fake"}))
    monkeypatch.setattr(jira_service.requests, "post", post)

    configured.exchange_code_for_token("auth-code-fake", "http://localhost:8501")

    assert post.last["kwargs"]["json"] == {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": "auth-code-fake",
        "redirect_uri": "http://localhost:8501",
    }


def test_code_exchange_does_not_send_a_pkce_verifier(configured, monkeypatch):
    post = RecordingPost(FakeResponse(payload={"access_token": "at-fake"}))
    monkeypatch.setattr(jira_service.requests, "post", post)

    configured.exchange_code_for_token(
        "auth-code-fake", "http://localhost:8501", code_verifier="verifier-fake"
    )

    assert "code_verifier" not in post.last["kwargs"]["json"]


def test_code_exchange_returns_the_token_payload(configured, monkeypatch):
    payload = {
        "access_token": "at-fake",
        "refresh_token": "rt-fake",
        "expires_in": 3600,
        "scope": "read:me offline_access",
        "token_type": "Bearer",
    }
    monkeypatch.setattr(
        jira_service.requests, "post", RecordingPost(FakeResponse(payload=payload))
    )

    assert configured.exchange_code_for_token("c", "http://localhost:8501") == payload


def test_code_exchange_requires_configuration():
    with pytest.raises(ProviderNotConfiguredError):
        JiraService().exchange_code_for_token("c", "http://localhost:8501")


# --- Token exchange failures ---------------------------------------------

def test_token_error_surfaces_the_provider_description(configured, monkeypatch):
    monkeypatch.setattr(
        jira_service.requests,
        "post",
        RecordingPost(
            FakeResponse(
                status_code=400,
                payload={"error": "invalid_client", "error_description": "Unknown client"},
            )
        ),
    )

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        configured.exchange_code_for_token("c", "http://localhost:8501")

    assert "Unknown client" in str(excinfo.value)


def test_invalid_grant_explains_the_rotating_refresh_token(configured, monkeypatch):
    monkeypatch.setattr(
        jira_service.requests,
        "post",
        RecordingPost(FakeResponse(status_code=400, payload={"error": "invalid_grant"})),
    )

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        configured.refresh_access_token("rt-stale-fake")

    message = str(excinfo.value)
    assert "invalid_grant" in message
    assert "already used" in message


def test_unparseable_error_body_still_raises_cleanly(configured, monkeypatch):
    monkeypatch.setattr(
        jira_service.requests,
        "post",
        RecordingPost(FakeResponse(status_code=500, raises=True)),
    )

    with pytest.raises(ProviderAuthenticationError):
        configured.exchange_code_for_token("c", "http://localhost:8501")


def test_success_response_without_an_access_token_is_rejected(configured, monkeypatch):
    monkeypatch.setattr(
        jira_service.requests,
        "post",
        RecordingPost(FakeResponse(payload={"token_type": "Bearer"})),
    )

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        configured.exchange_code_for_token("c", "http://localhost:8501")

    assert "did not include an access token" in str(excinfo.value)


def test_network_failure_becomes_an_authentication_error(configured, monkeypatch):
    def boom(*args, **kwargs):
        raise jira_service.requests.RequestException("connection reset")

    monkeypatch.setattr(jira_service.requests, "post", boom)

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        configured.exchange_code_for_token("c", "http://localhost:8501")

    assert "Network error" in str(excinfo.value)


def test_token_errors_never_echo_the_client_secret(configured, monkeypatch):
    monkeypatch.setattr(
        jira_service.requests,
        "post",
        RecordingPost(
            FakeResponse(status_code=401, payload={"error_description": "bad credentials"})
        ),
    )

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        configured.exchange_code_for_token("auth-code-fake", "http://localhost:8501")

    message = str(excinfo.value)
    assert CLIENT_SECRET not in message
    assert "auth-code-fake" not in message


# --- Refresh --------------------------------------------------------------

def test_refresh_sends_the_documented_body_fields(configured, monkeypatch):
    post = RecordingPost(FakeResponse(payload={"access_token": "at-new-fake"}))
    monkeypatch.setattr(jira_service.requests, "post", post)

    configured.refresh_access_token("rt-fake")

    assert post.last["kwargs"]["json"] == {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": "rt-fake",
    }
    assert "data" not in post.last["kwargs"]


def test_refresh_without_a_token_fails_before_any_request(configured, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no request should be sent without a refresh token")

    monkeypatch.setattr(jira_service.requests, "post", boom)

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        configured.refresh_access_token("")

    assert "no refresh token" in str(excinfo.value)


def test_rotated_refresh_token_replaces_the_previous_one():
    """
    Atlassian disables the refresh token that was just exchanged, so the
    replacement in the response must win over the stored one.
    """
    original = TokenSet.from_response(
        {"access_token": "at-1-fake", "refresh_token": "rt-1-fake", "expires_in": 3600}
    )
    rotated = TokenSet.from_response(
        {"access_token": "at-2-fake", "refresh_token": "rt-2-fake", "expires_in": 3600},
        previous=original,
    )

    assert rotated.refresh_token == "rt-2-fake"


def test_scopes_are_retained_when_a_refresh_response_omits_them():
    original = TokenSet.from_response(
        {
            "access_token": "at-1-fake",
            "refresh_token": "rt-1-fake",
            "scope": "read:me offline_access",
        }
    )
    refreshed = TokenSet.from_response(
        {"access_token": "at-2-fake", "refresh_token": "rt-2-fake"}, previous=original
    )

    assert refreshed.scopes == ("read:me", "offline_access")


# --- Identity -------------------------------------------------------------

def test_user_profile_reads_the_atlassian_me_endpoint(configured, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(payload={"account_id": "acc-1", "email": "person@example.test"})

    monkeypatch.setattr(jira_service.requests, "get", fake_get)

    profile = configured.get_user_profile("at-fake")

    assert calls[0]["url"] == "https://api.atlassian.com/me"
    assert calls[0]["kwargs"]["headers"] == {"Authorization": "Bearer at-fake"}
    assert profile["email"] == "person@example.test"


def test_user_profile_is_empty_rather_than_raising_on_failure(configured, monkeypatch):
    def boom(*args, **kwargs):
        raise jira_service.requests.RequestException("no route to host")

    monkeypatch.setattr(jira_service.requests, "get", boom)

    assert configured.get_user_profile("at-fake") == {}


def test_user_profile_ignores_a_non_200_response(configured, monkeypatch):
    monkeypatch.setattr(
        jira_service.requests,
        "get",
        lambda url, **kwargs: FakeResponse(status_code=403, payload={"message": "forbidden"}),
    )

    assert configured.get_user_profile("at-fake") == {}


# --- Tokens are never displayed ------------------------------------------

def test_public_summary_exposes_no_token_material():
    tokens = TokenSet.from_response(
        {
            "access_token": "at-secret-fake",
            "refresh_token": "rt-secret-fake",
            "expires_in": 3600,
            "scope": "read:me offline_access",
        }
    )

    rendered = repr(tokens.public_summary()) + repr(tokens)

    assert "at-secret-fake" not in rendered
    assert "rt-secret-fake" not in rendered
    assert tokens.public_summary()["has_refresh_token"] is True


# --- Wiring into the shared callback -------------------------------------

class StubJira:
    """Stands in for JiraService inside main.PROVIDER_CLASSES."""

    name = "jira"
    display_name = "Jira Cloud"
    post_connect_hint = "Jira is connected. Nothing has been created in Jira."

    exchanges = []
    response = {"access_token": "at-fake", "refresh_token": "rt-fake", "expires_in": 3600}
    failure = None

    def exchange_code_for_token(self, code, redirect_uri, code_verifier=None):
        type(self).exchanges.append(code)
        if type(self).failure is not None:
            raise type(self).failure
        return type(self).response


@pytest.fixture
def jira_callback(monkeypatch):
    StubJira.exchanges = []
    StubJira.failure = None
    st.query_params.clear()
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)

    monkeypatch.setitem(main.PROVIDER_CLASSES, "jira", StubJira)
    yield StubJira
    st.query_params.clear()
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)


def flash_text():
    return " ".join(message for _, message in (st.session_state.get("_flashes") or []))


def test_jira_is_registered_for_callback_routing():
    assert main.PROVIDER_CLASSES["jira"] is JiraService


def test_verified_callback_stores_a_jira_session(jira_callback):
    handshake = begin_handshake("jira")
    st.query_params["code"] = "auth-code-fake"
    st.query_params["state"] = handshake.state

    main._handle_oauth_callback()

    tokens = st.session_state[main._skey("jira", "tokens")]
    assert isinstance(tokens, TokenSet)
    assert jira_callback.exchanges == ["auth-code-fake"]


def test_connect_success_message_is_not_the_transcript_source_wording(jira_callback):
    handshake = begin_handshake("jira")
    st.query_params["code"] = "auth-code-fake"
    st.query_params["state"] = handshake.state

    main._handle_oauth_callback()

    message = flash_text()
    assert "Connected to Jira Cloud" in message
    assert "Transcript Source" not in message


def test_callback_never_flashes_the_code_or_a_token(jira_callback):
    handshake = begin_handshake("jira")
    st.query_params["code"] = "auth-code-fake"
    st.query_params["state"] = handshake.state

    main._handle_oauth_callback()

    message = flash_text()
    assert "auth-code-fake" not in message
    assert "at-fake" not in message
    assert "rt-fake" not in message


def test_tampered_state_is_rejected_without_an_exchange(jira_callback):
    handshake = begin_handshake("jira")
    st.query_params["code"] = "auth-code-fake"
    st.query_params["state"] = handshake.state[:-4] + "AAAA"

    main._handle_oauth_callback()

    assert jira_callback.exchanges == []
    assert main._skey("jira", "tokens") not in st.session_state
    assert "could not be verified" in flash_text()


def test_expired_state_is_rejected_without_an_exchange(jira_callback):
    import time

    from providers.oauth_state import STATE_TTL_SECONDS

    stale = begin_handshake("jira", now=time.time() - STATE_TTL_SECONDS - 60)
    st.query_params["code"] = "auth-code-fake"
    st.query_params["state"] = stale.state

    main._handle_oauth_callback()

    assert jira_callback.exchanges == []
    assert main._skey("jira", "tokens") not in st.session_state
    assert "expired" in flash_text()


def test_denied_authorization_connects_nothing(jira_callback):
    st.query_params["error"] = "access_denied"

    main._handle_oauth_callback()

    assert jira_callback.exchanges == []
    assert main._skey("jira", "tokens") not in st.session_state
    assert "declined" in flash_text()


def test_exchange_failure_leaves_no_session(jira_callback):
    jira_callback.failure = ProviderAuthenticationError("Jira authorization failed: invalid_client")
    handshake = begin_handshake("jira")
    st.query_params["code"] = "auth-code-fake"
    st.query_params["state"] = handshake.state

    main._handle_oauth_callback()

    assert main._skey("jira", "tokens") not in st.session_state
    assert "could not be completed" in flash_text()


def test_missing_configuration_is_reported_by_name_only(jira_callback):
    """A real JiraService in the callback path must fail with names, not values."""
    handshake = begin_handshake("jira")
    st.query_params["code"] = "auth-code-fake"
    st.query_params["state"] = handshake.state
    st.session_state.pop(main._skey("jira", "tokens"), None)

    main.PROVIDER_CLASSES["jira"] = JiraService
    try:
        main._handle_oauth_callback()
    finally:
        main.PROVIDER_CLASSES["jira"] = StubJira

    message = flash_text()
    assert "JIRA_CLIENT_ID" in message
    assert main._skey("jira", "tokens") not in st.session_state


def test_disconnect_clears_the_jira_session(jira_callback):
    service = StubJira()
    st.session_state[main._skey("jira", "tokens")] = TokenSet.from_response(
        {"access_token": "at-fake", "expires_in": 3600}
    )
    st.session_state[main._skey("jira", "identity")] = "person@example.test"

    main._disconnect(service)

    assert main._skey("jira", "tokens") not in st.session_state
    assert main._skey("jira", "identity") not in st.session_state


def test_jira_session_is_namespaced_away_from_the_transcript_providers():
    assert main._skey("jira", "tokens") not in (
        main._skey("google_meet", "tokens"),
        main._skey("microsoft_teams", "tokens"),
    )


# --- Connected / disconnected UI state -----------------------------------

def test_section_renders_the_unconfigured_state(jira_callback):
    """Importing main already exercises this path; assert it stays exception-free."""
    main._render_jira_section()


def test_section_renders_a_connect_button_when_configured(jira_callback, monkeypatch):
    monkeypatch.setenv(jira_config.CLIENT_ID_VAR, CLIENT_ID)
    monkeypatch.setenv(jira_config.CLIENT_SECRET_VAR, CLIENT_SECRET)
    captions = []
    monkeypatch.setattr(main.st, "caption", lambda text, *a, **k: captions.append(text))
    links = []
    monkeypatch.setattr(
        main.st, "link_button", lambda label, url, **k: links.append((label, url))
    )

    main._render_jira_section()

    assert links and links[0][0] == "Connect Jira Cloud"
    assert links[0][1].startswith("https://auth.atlassian.com/authorize?")
    # The Jira caption must not claim a PKCE challenge that was not sent.
    joined = " ".join(captions)
    assert "PKCE" not in joined
    assert CLIENT_SECRET not in joined + links[0][1]


def test_section_renders_the_connected_state(jira_callback, monkeypatch):
    monkeypatch.setenv(jira_config.CLIENT_ID_VAR, CLIENT_ID)
    monkeypatch.setenv(jira_config.CLIENT_SECRET_VAR, CLIENT_SECRET)
    monkeypatch.setattr(
        jira_service.requests, "get", lambda url, **k: FakeResponse(status_code=403)
    )
    st.session_state[main._skey("jira", "tokens")] = TokenSet.from_response(
        {
            "access_token": "at-secret-fake",
            "refresh_token": "rt-secret-fake",
            "expires_in": 3600,
            "scope": "read:me offline_access",
        }
    )
    shown = []
    for widget in ("caption", "success", "write", "markdown"):
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, **k: shown.append(str(text))
        )
    monkeypatch.setattr(main.st, "link_button", lambda *a, **k: pytest.fail("still disconnected"))

    main._render_jira_section()

    rendered = " ".join(shown)
    assert "Connected to Jira Cloud" in rendered
    assert "at-secret-fake" not in rendered
    assert "rt-secret-fake" not in rendered


# --- JIRA-002 boundary ----------------------------------------------------

def test_service_cannot_reach_jira_or_create_anything_yet():
    """
    JIRA-002 is the connection only. Site discovery, project discovery, metadata
    and issue creation arrive in later tickets, so none of them exist here.
    """
    for absent in (
        "get_accessible_resources",
        "list_sites",
        "list_projects",
        "get_create_metadata",
        "create_issue",
        "create_issues",
    ):
        assert not hasattr(JiraService, absent), absent


def test_jira_is_not_offered_as_a_transcript_source():
    """Jira must not appear in the transcript funnel."""
    assert not hasattr(JiraService, "list_available_transcripts")
    assert not hasattr(JiraService, "get_transcript")
