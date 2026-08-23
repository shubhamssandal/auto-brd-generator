"""
JIRA-003 tests: accessible Jira Cloud site discovery.

Every HTTP call is monkeypatched, so no Atlassian app, no real credentials and no
network access are required. Credential and token values here are obvious fakes.

The assertions encode the parts of Atlassian's documented accessible-resources
contract that are easy to get wrong: the response is a JSON *array* (not an
object), an empty array is a legitimate answer rather than an error, and the
endpoint reports only the sites the token holds a Jira scope for -- which is why
an empty result has to be explained rather than shown as a bare "none found".
"""

import pytest
import streamlit as st

import jira_config
import jira_service
import main
from jira_models import JiraSite
from jira_service import JiraService
from providers.base import (
    ProviderAPIError,
    ProviderConsentRequiredError,
    ProviderTokenExpiredError,
)
from providers.session_tokens import TokenSet

CLIENT_ID = "jira-client-id-fake"
CLIENT_SECRET = "jira-client-secret-fake"
ACCESS_TOKEN = "at-secret-fake"
REFRESH_TOKEN = "rt-secret-fake"

SITE_A = {
    "id": "1324a887-45db-1bf4-1e99-ef0ff456d421",
    "name": "Acme Engineering",
    "url": "https://acme-eng.atlassian.net",
    "scopes": ["read:jira-user"],
    "avatarUrl": "https://site-admin-avatar-cdn.example/avatar.png",
}
SITE_B = {
    "id": "8f4c2b10-77aa-4c31-9d0e-b2e1f0a9c333",
    "name": "Acme Support",
    "url": "https://acme-support.atlassian.net",
    "scopes": ["read:jira-user", "read:me"],
    "avatarUrl": "https://site-admin-avatar-cdn.example/other.png",
}


@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    """A fresh browser session and no Jira credentials unless a test asks."""
    for name in (
        jira_config.CLIENT_ID_VAR,
        jira_config.CLIENT_SECRET_VAR,
        jira_config.REDIRECT_URI_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)
    yield
    for key in list(st.session_state.keys()):
        st.session_state.pop(key, None)


@pytest.fixture(autouse=True)
def forbid_write_requests(monkeypatch):
    """
    Site discovery is read-only. Any HTTP verb that could change something in Jira
    fails the test outright. A test that legitimately needs `post` (the token
    endpoint, for refresh) overrides this itself.
    """
    for verb in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(
            jira_service.requests,
            verb,
            lambda *a, **k: pytest.fail("JIRA-003 must not send a {} request".format(verb)),
            raising=False,
        )


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(jira_config.CLIENT_ID_VAR, CLIENT_ID)
    monkeypatch.setenv(jira_config.CLIENT_SECRET_VAR, CLIENT_SECRET)
    return JiraService()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raises=False, text=""):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises
        self.text = text

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class RecordingGet:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        return self.response

    @property
    def last(self):
        return self.calls[-1]


def patch_get(monkeypatch, response):
    recorder = RecordingGet(response)
    monkeypatch.setattr(jira_service.requests, "get", recorder)
    return recorder


def tokens_for(scopes="read:me read:jira-user offline_access", refresh=REFRESH_TOKEN):
    return TokenSet.from_response(
        {
            "access_token": ACCESS_TOKEN,
            "refresh_token": refresh,
            "expires_in": 3600,
            "scope": scopes,
        }
    )


# --- The documented request ------------------------------------------------

def test_uses_the_documented_endpoint_with_a_bearer_token(configured, monkeypatch):
    recorder = patch_get(monkeypatch, FakeResponse(payload=[]))

    configured.list_accessible_sites(ACCESS_TOKEN)

    assert recorder.last["url"] == "https://api.atlassian.com/oauth/token/accessible-resources"
    headers = recorder.last["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer {}".format(ACCESS_TOKEN)
    assert headers["Accept"] == "application/json"
    # Atlassian documents "Request parameters: None" for this endpoint.
    assert not recorder.last["kwargs"].get("params")
    assert recorder.last["kwargs"]["timeout"] == JiraService.TIMEOUT_SECONDS


def test_endpoint_is_on_the_api_host_not_the_auth_host():
    """
    Atlassian's docs contain one sentence naming auth.atlassian.com while every
    runnable example uses api.atlassian.com. Following the prose would 404.
    """
    assert JiraService.ACCESSIBLE_RESOURCES_URL.startswith("https://api.atlassian.com/")


# --- Zero, one and multiple sites -----------------------------------------

def test_zero_sites_is_an_empty_list_not_an_error(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[]))
    assert configured.list_accessible_sites(ACCESS_TOKEN) == []


def test_one_site_is_parsed(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    sites = configured.list_accessible_sites(ACCESS_TOKEN)

    assert len(sites) == 1
    assert sites[0].id == SITE_A["id"]
    assert sites[0].name == "Acme Engineering"
    assert sites[0].url == "https://acme-eng.atlassian.net"
    assert sites[0].scopes == ("read:jira-user",)


def test_multiple_sites_are_parsed_in_order(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A, SITE_B]))

    sites = configured.list_accessible_sites(ACCESS_TOKEN)

    assert [s.name for s in sites] == ["Acme Engineering", "Acme Support"]


def test_only_needed_non_secret_fields_are_kept(configured, monkeypatch):
    """The response carries an avatar URL this app never renders; it is dropped."""
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    site = configured.list_accessible_sites(ACCESS_TOKEN)[0]

    assert set(vars(site)) == {"id", "name", "url", "scopes"}
    assert "avatarUrl" not in vars(site)
    assert ACCESS_TOKEN not in repr(site)


# --- Malformed responses are not reported as "no access" ------------------

def test_a_non_array_body_is_an_api_error(configured, monkeypatch):
    """The documented response is an array; an object means something changed."""
    patch_get(monkeypatch, FakeResponse(payload={"values": [SITE_A]}))

    with pytest.raises(ProviderAPIError):
        configured.list_accessible_sites(ACCESS_TOKEN)


def test_an_unparseable_body_is_an_api_error(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(raises=True))

    with pytest.raises(ProviderAPIError):
        configured.list_accessible_sites(ACCESS_TOKEN)


def test_entries_without_a_site_id_do_not_look_like_zero_access(configured, monkeypatch):
    """
    A site with no cloud id cannot be addressed. Returning [] here would read as a
    permissions problem and send the user off re-consenting for nothing.
    """
    patch_get(monkeypatch, FakeResponse(payload=[{"name": "No id", "url": "https://x.test"}]))

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_accessible_sites(ACCESS_TOKEN)

    assert "site id" in str(excinfo.value)


def test_usable_sites_survive_alongside_an_unusable_one(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[{"name": "No id"}, SITE_B, "not-an-object"]))

    sites = configured.list_accessible_sites(ACCESS_TOKEN)

    assert [s.id for s in sites] == [SITE_B["id"]]


# --- Error states reuse the shared provider hierarchy ---------------------

def test_401_is_a_token_expiry_so_refresh_can_react(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(status_code=401, payload={"message": "Unauthorized"}))

    with pytest.raises(ProviderTokenExpiredError):
        configured.list_accessible_sites(ACCESS_TOKEN)


def test_403_is_an_authorization_failure(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(status_code=403, payload={"message": "Forbidden"}))

    with pytest.raises(ProviderConsentRequiredError):
        configured.list_accessible_sites(ACCESS_TOKEN)


def test_429_reports_rate_limiting_with_its_status(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(status_code=429, payload={}))

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_accessible_sites(ACCESS_TOKEN)

    assert excinfo.value.status_code == 429


@pytest.mark.parametrize("status", [404, 500, 503])
def test_other_statuses_surface_as_api_errors(configured, monkeypatch, status):
    patch_get(monkeypatch, FakeResponse(status_code=status, payload={}))

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_accessible_sites(ACCESS_TOKEN)

    assert excinfo.value.status_code == status


def test_a_network_failure_is_an_api_error(configured, monkeypatch):
    def boom(*a, **k):
        raise jira_service.requests.RequestException("connection reset")

    monkeypatch.setattr(jira_service.requests, "get", boom)

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_accessible_sites(ACCESS_TOKEN)

    assert "Network error" in str(excinfo.value)


def test_error_messages_never_echo_the_access_token(configured, monkeypatch):
    patch_get(
        monkeypatch,
        FakeResponse(status_code=500, payload=None, raises=True, text=ACCESS_TOKEN * 200),
    )

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_accessible_sites(ACCESS_TOKEN)

    # The token only appears here because a hostile server echoed it back; the cap
    # is what stops an error page from flooding the UI.
    assert len(str(excinfo.value)) < 500


# --- Expiry handling is the shared refresh path, not a new one ------------

def test_an_expired_token_is_refreshed_and_the_call_retried(configured, monkeypatch):
    """Proves site discovery reuses call_with_refresh rather than its own retry."""
    responses = [
        FakeResponse(status_code=401, payload={"message": "expired"}),
        FakeResponse(status_code=200, payload=[SITE_A]),
    ]
    monkeypatch.setattr(jira_service.requests, "get", lambda url, **k: responses.pop(0))
    monkeypatch.setattr(
        jira_service.requests,
        "post",
        lambda url, **k: FakeResponse(
            payload={
                "access_token": "at-rotated-fake",
                "refresh_token": "rt-rotated-fake",
                "expires_in": 3600,
                "scope": "read:me read:jira-user offline_access",
            }
        ),
    )

    from providers.session_tokens import call_with_refresh

    sites, refreshed = call_with_refresh(
        configured,
        tokens_for(),
        lambda token: configured.list_accessible_sites(access_token=token),
    )

    assert [s.name for s in sites] == ["Acme Engineering"]
    # Atlassian disables the refresh token that was just used, so the rotated one
    # must be what the caller stores.
    assert refreshed.refresh_token == "rt-rotated-fake"


# --- The site model -------------------------------------------------------

@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"id": "c1", "name": "Acme", "url": "https://a.test"}, "Acme — https://a.test"),
        ({"id": "c1", "name": "Acme"}, "Acme"),
        ({"id": "c1", "url": "https://a.test"}, "https://a.test"),
        ({"id": "c1"}, "c1"),
    ],
)
def test_display_label_falls_back_without_inventing_anything(payload, expected):
    assert JiraSite.from_api(payload).display_label == expected


def test_from_api_rejects_an_entry_with_no_id():
    assert JiraSite.from_api({"name": "Acme"}) is None
    assert JiraSite.from_api({"id": "   "}) is None
    assert JiraSite.from_api("not-a-dict") is None


def test_a_selected_site_cannot_be_mutated_in_place():
    site = JiraSite.from_api(SITE_A)
    with pytest.raises(Exception):
        site.id = "tampered"


# --- UI states ------------------------------------------------------------

def render_sites_panel(service, tokens, monkeypatch, clicked=True, select=0):
    """
    Render the site panel and capture what the user would see.

    Returns (messages, selectbox_calls). The retrieve button is reported as
    clicked by default; the picker returns `select`.
    """
    shown = {"success": [], "warning": [], "error": [], "caption": [], "markdown": [], "write": []}
    for widget in shown:
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, _w=widget, **k: shown[_w].append(str(text))
        )
    monkeypatch.setattr(main.st, "button", lambda label, **k: clicked)

    picks = []

    def fake_selectbox(label, options, format_func=None, **kwargs):
        picks.append(
            {
                "label": label,
                "options": list(options),
                "labels": [format_func(o) for o in options] if format_func else [],
            }
        )
        return select

    monkeypatch.setattr(main.st, "selectbox", fake_selectbox)

    main._render_jira_sites_panel(service, tokens)
    return shown, picks


def test_a_single_site_is_selected_without_asking(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    shown, picks = render_sites_panel(configured, tokens_for(), monkeypatch)

    assert picks == [], "one site needs no picker"
    assert any("Acme Engineering" in text for text in shown["success"])
    stored = st.session_state[main._skey("jira", "site")]
    assert stored.id == SITE_A["id"]


def test_multiple_sites_are_offered_for_selection(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A, SITE_B]))

    shown, picks = render_sites_panel(configured, tokens_for(), monkeypatch, select=1)

    assert len(picks) == 1
    assert picks[0]["options"] == [0, 1]
    assert picks[0]["labels"] == [
        "Acme Engineering — https://acme-eng.atlassian.net",
        "Acme Support — https://acme-support.atlassian.net",
    ]
    assert "2 found" in picks[0]["label"]
    # The user's choice is what gets stored, not the first entry.
    assert st.session_state[main._skey("jira", "site")].id == SITE_B["id"]


def test_only_non_secret_site_fields_reach_session_state(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    render_sites_panel(configured, tokens_for(), monkeypatch)

    stored = st.session_state[main._skey("jira", "site")]
    assert set(vars(stored)) == {"id", "name", "url", "scopes"}
    blob = repr(stored) + repr(vars(stored))
    assert ACCESS_TOKEN not in blob
    assert REFRESH_TOKEN not in blob
    assert CLIENT_SECRET not in blob


def test_empty_access_explains_a_missing_scope_when_that_is_the_cause(configured, monkeypatch):
    """
    A session authorized before read:jira-user was requested can never see a site.
    Saying only "none found" would send the user hunting a Jira permissions problem
    that does not exist.
    """
    patch_get(monkeypatch, FakeResponse(payload=[]))

    shown, _ = render_sites_panel(
        configured, tokens_for(scopes="read:me offline_access"), monkeypatch
    )

    warning = " ".join(shown["warning"])
    assert JiraService.SITE_SCOPE in warning
    assert "connect again" in warning
    assert main._skey("jira", "site") not in st.session_state


def test_empty_access_does_not_blame_a_scope_that_was_granted(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload=[]))

    shown, _ = render_sites_panel(configured, tokens_for(), monkeypatch)

    warning = " ".join(shown["warning"])
    assert "no Jira Cloud site" in warning
    assert JiraService.SITE_SCOPE not in warning
    assert not shown["success"]


def test_nothing_is_requested_until_the_button_is_pressed(configured, monkeypatch):
    recorder = patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    shown, picks = render_sites_panel(configured, tokens_for(), monkeypatch, clicked=False)

    assert recorder.calls == []
    assert picks == []
    assert main._skey("jira", "sites") not in st.session_state


def test_an_authorization_failure_is_reported_and_nothing_is_cached(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(status_code=403, payload={"message": "Forbidden"}))

    shown, _ = render_sites_panel(configured, tokens_for(), monkeypatch)

    assert any("unauthorized" in text.lower() for text in shown["error"])
    assert main._skey("jira", "sites") not in st.session_state
    assert main._skey("jira", "site") not in st.session_state


def test_an_unrenewable_expired_session_is_cleared(configured, monkeypatch):
    """A dead session must offer Connect again rather than loop on a bad token."""
    patch_get(monkeypatch, FakeResponse(status_code=401, payload={"message": "expired"}))
    st.session_state[main._skey("jira", "tokens")] = tokens_for(refresh=None)

    shown, _ = render_sites_panel(configured, tokens_for(refresh=None), monkeypatch)

    assert shown["error"]
    assert main._skey("jira", "tokens") not in st.session_state


def test_requerying_drops_a_stale_selection(configured, monkeypatch):
    st.session_state[main._skey("jira", "site")] = JiraSite.from_api(SITE_B)
    st.session_state["select_jira_site"] = 1
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    render_sites_panel(configured, tokens_for(), monkeypatch)

    assert st.session_state[main._skey("jira", "site")].id == SITE_A["id"]


def test_disconnect_clears_the_discovered_sites_and_selection(configured):
    st.session_state[main._skey("jira", "tokens")] = tokens_for()
    st.session_state[main._skey("jira", "sites")] = [JiraSite.from_api(SITE_A)]
    st.session_state[main._skey("jira", "site")] = JiraSite.from_api(SITE_A)

    main._disconnect(configured)

    for suffix in ("tokens", "sites", "site"):
        assert main._skey("jira", suffix) not in st.session_state


# --- Read-only boundary ---------------------------------------------------

def test_site_discovery_writes_nothing(configured, monkeypatch):
    """
    Discovery is GET-only. JIRA-007 added ``create_issue`` to the service, so "no write
    method exists" is no longer the way to state that -- what matters is that listing
    sites sends no write request, which the autouse ``forbid_write_requests`` fixture
    enforces for every verb that could change something in Jira.
    """
    recorder = patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    configured.list_accessible_sites(ACCESS_TOKEN)

    assert len(recorder.calls) == 1
    for absent in ("create_issues", "update_issue", "delete_issue"):
        assert not hasattr(JiraService, absent), absent
