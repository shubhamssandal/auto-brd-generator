"""
JIRA-004 tests: Jira project and create-metadata discovery.

Every HTTP call is monkeypatched, so no Atlassian app, no real credentials and no
network access are required. Credential and token values here are obvious fakes.

The harness is imported from ``test_jira_sites`` rather than rebuilt: the autouse
fixtures there already give each test a clean session, no Jira environment, and a
hard failure on any write verb, which is exactly the read-only guarantee this
ticket needs.

Two things drive what is asserted. First, the endpoint paths and response envelope
for project search and create-metadata could not be verified against Atlassian's
reference (their v3 pages render client-side and the published OpenAPI document
truncates before ``paths``), so the tests pin the *tolerance* the implementation
was given -- a bare array, a missing ``isLast``, an absent ``total`` -- instead of
pinning one guessed shape. Second, ``PRD.md`` forbids assuming that every project
uses the same hierarchy, so the tests check that Jira's own ``subtask`` and
``hierarchyLevel`` are reported and that no level is renamed into Epic or Story.
"""

import pytest
import streamlit as st

import jira_service
import main
from jira_models import JiraProject, JiraSite
from jira_service import JiraService
from providers.base import (
    ProviderAPIError,
    ProviderConsentRequiredError,
    ProviderTokenExpiredError,
)
from providers.session_tokens import call_with_refresh
from test_jira_sites import (  # noqa: F401 -- fixtures are used by pytest, not by name
    ACCESS_TOKEN,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    SITE_A,
    SITE_B,
    FakeResponse,
    clean_session,
    configured,
    forbid_write_requests,
    patch_get,
    tokens_for,
)

# The scope set this app now requests. `tokens_for` still defaults to the narrower
# JIRA-003 grant, which is what the missing-scope tests below need.
FULL_SCOPES = "read:me read:jira-user read:jira-work offline_access"

SITE = JiraSite.from_api(SITE_A)
OTHER_SITE = JiraSite.from_api(SITE_B)

# A project search entry carrying more than this app keeps, so the "only what is
# needed" assertion has something to actually drop.
PROJECT_ENG = {
    "id": "10001",
    "key": "ENG",
    "name": "Engineering",
    "projectTypeKey": "software",
    "simplified": False,
    "style": "classic",
    "isPrivate": False,
    "uuid": "b8b4e5a1-0000-0000-0000-000000000001",
    "avatarUrls": {"48x48": "https://avatar.example/eng.png"},
}
PROJECT_OPS = {"id": "10002", "key": "OPS", "name": "Operations", "projectTypeKey": "business"}

TYPE_STORY = {
    "id": "10100",
    "name": "Story",
    "subtask": False,
    "hierarchyLevel": 0,
    "description": "A unit of work described from a user's point of view.",
}
TYPE_SUBTASK = {"id": "10101", "name": "Sub-task", "subtask": True, "hierarchyLevel": -1}
# Deliberately not a name Jira's defaults use, at a level nothing should try to
# label: if anything renames hierarchy levels, this is what exposes it.
TYPE_WIDGET = {"id": "10102", "name": "Widget", "subtask": False, "hierarchyLevel": 3}

FIELD_SUMMARY = {
    "fieldId": "summary",
    "name": "Summary",
    "required": True,
    "schema": {"type": "string"},
}
FIELD_REPORTER = {
    "fieldId": "reporter",
    "name": "Reporter",
    "required": True,
    "hasDefaultValue": True,
}
FIELD_SQUAD = {
    "fieldId": "customfield_10101",
    "name": "Squad",
    "required": True,
    "schema": {"type": "option"},
}
FIELD_LABELS = {"fieldId": "labels", "name": "Labels", "required": False}


def jira_tokens(scopes=FULL_SCOPES, refresh=REFRESH_TOKEN):
    """A session holding the scopes this ticket requests, unless a test narrows them."""
    return tokens_for(scopes=scopes, refresh=refresh)


def patch_routes(monkeypatch, handler):
    """
    Route GETs by URL. ``handler(url, params)`` returns the response.

    Needed because create-metadata discovery walks three different endpoints in one
    operation, which the single-response recorder in ``test_jira_sites`` cannot
    express. Returns the recorded calls.
    """
    calls = []

    def get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return handler(url, kwargs.get("params") or {})

    monkeypatch.setattr(jira_service.requests, "get", get)
    return calls


def metadata_routes(issue_types, fields_by_type):
    """
    A handler serving the create-metadata endpoints.

    An issue type absent from ``fields_by_type`` answers its field lookup with a
    500, which is how the "one awkward issue type must not lose the project" and
    "unknown is not ok" cases are set up.
    """

    def handler(url, params):
        if "/issuetypes/" in url:
            type_id = url.rsplit("/", 1)[-1]
            fields = fields_by_type.get(type_id)
            if fields is None:
                return FakeResponse(status_code=500, payload={"message": "field lookup failed"})
            return FakeResponse(payload={"isLast": True, "values": fields})
        if url.endswith("/issuetypes"):
            return FakeResponse(payload={"isLast": True, "values": issue_types})
        return FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG]})

    return handler


def render_projects_panel(
    service,
    tokens,
    monkeypatch,
    site=SITE,
    clicked=("discover_jira_projects",),
    select=0,
):
    """
    Render the project panel and capture what the user would see.

    Returns ``(messages, selectbox_calls)``. ``clicked`` names the buttons reported
    as pressed by key, so a test can retrieve projects without also reading
    metadata. Mirrors ``render_sites_panel`` in ``test_jira_sites``.
    """
    shown = {
        key: []
        for key in ("success", "warning", "error", "info", "caption", "markdown", "write")
    }
    for widget in shown:
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, _w=widget, **k: shown[_w].append(str(text))
        )
    monkeypatch.setattr(main.st, "button", lambda label, **k: k.get("key") in clicked)

    # Per-issue-type detail is rendered inside an expander, whose header carries
    # the hierarchy note. Record the header, then hand off to the real expander,
    # which works unpatched in pytest's bare mode.
    shown["expander"] = []
    real_expander = main.st.expander

    def recording_expander(label, *args, **kwargs):
        shown["expander"].append(str(label))
        return real_expander(label, *args, **kwargs)

    monkeypatch.setattr(main.st, "expander", recording_expander)

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

    main._render_jira_projects_panel(service, tokens, site)
    return shown, picks


# --- Site-scoped request construction --------------------------------------

def test_project_search_is_addressed_to_the_selected_site(configured, monkeypatch):
    """
    3LO tokens go to api.atlassian.com/ex/jira/{cloudid}; Atlassian states they
    must not be sent to a site's own atlassian.net host.
    """
    recorder = patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": []}))

    configured.list_projects(ACCESS_TOKEN, SITE.id)

    url = recorder.last["url"]
    assert url == "https://api.atlassian.com/ex/jira/{}/rest/api/3/project/search".format(SITE.id)
    assert "atlassian.net" not in url
    headers = recorder.last["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer {}".format(ACCESS_TOKEN)
    assert headers["Accept"] == "application/json"
    assert recorder.last["kwargs"]["params"]["startAt"] == 0
    assert recorder.last["kwargs"]["params"]["maxResults"] == JiraService.PAGE_SIZE
    assert recorder.last["kwargs"]["timeout"] == JiraService.TIMEOUT_SECONDS


def test_a_cloud_id_cannot_escape_its_path_segment(configured, monkeypatch):
    """The cloud id arrives from an API response and lands in a URL path."""
    recorder = patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": []}))

    configured.list_projects(ACCESS_TOKEN, "../../evil")

    assert "/ex/jira/..%2F..%2Fevil/" in recorder.last["url"]


@pytest.mark.parametrize("cloud_id", ["", "   ", None])
def test_no_selected_site_is_refused_before_any_request(configured, monkeypatch, cloud_id):
    recorder = patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": []}))

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_projects(ACCESS_TOKEN, cloud_id)

    assert "No Jira site is selected" in str(excinfo.value)
    assert recorder.calls == [], "nothing should be asked of Jira without a site"


def test_metadata_is_addressed_per_project_and_per_issue_type(configured, monkeypatch):
    calls = patch_routes(
        monkeypatch, metadata_routes([TYPE_STORY], {TYPE_STORY["id"]: [FIELD_SUMMARY]})
    )

    configured.get_project_metadata(ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"])

    base = "https://api.atlassian.com/ex/jira/{}/rest/api/3/issue/createmeta/{}".format(
        SITE.id, PROJECT_ENG["id"]
    )
    urls = [call["url"] for call in calls]
    assert base + "/issuetypes" in urls
    assert base + "/issuetypes/" + TYPE_STORY["id"] in urls


# --- Zero, one and multiple projects ---------------------------------------

def test_zero_projects_is_an_empty_result_not_an_error(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": []}))

    result = configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert result["projects"] == []
    assert result["truncated"] is False
    assert result["notes"] == []


def test_a_single_project_is_selected_without_asking(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG]}))

    shown, picks = render_projects_panel(configured, jira_tokens(), monkeypatch)

    assert picks == [], "one project needs no picker"
    assert any("Engineering (ENG)" in text for text in shown["success"])
    assert st.session_state[main._skey("jira", "project")].id == PROJECT_ENG["id"]


def test_multiple_projects_are_offered_for_selection(configured, monkeypatch):
    patch_get(
        monkeypatch, FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG, PROJECT_OPS]})
    )

    shown, picks = render_projects_panel(configured, jira_tokens(), monkeypatch, select=1)

    assert len(picks) == 1
    assert picks[0]["labels"] == ["Engineering (ENG)", "Operations (OPS)"]
    assert "2 found" in picks[0]["label"]
    # The user's choice is what gets stored, not the first entry.
    assert st.session_state[main._skey("jira", "project")].id == PROJECT_OPS["id"]


def test_only_needed_non_secret_project_fields_reach_session_state(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG]}))

    render_projects_panel(configured, jira_tokens(), monkeypatch)

    stored = st.session_state[main._skey("jira", "project")]
    assert set(vars(stored)) == {"id", "key", "name", "project_type_key"}
    blob = repr(stored) + repr(vars(stored))
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in blob


def test_nothing_is_requested_until_the_button_is_pressed(configured, monkeypatch):
    recorder = patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG]}))

    shown, picks = render_projects_panel(configured, jira_tokens(), monkeypatch, clicked=())

    assert recorder.calls == []
    assert picks == []
    assert main._skey("jira", "projects") not in st.session_state


# --- Pagination and safe truncation ----------------------------------------

def paged(monkeypatch, pages):
    """Serve ``pages`` in order, one per request, keyed by the requested startAt."""
    seen = []

    def handler(url, params):
        seen.append(params.get("startAt"))
        index = min(len(seen) - 1, len(pages) - 1)
        return FakeResponse(payload=pages[index])

    patch_routes(monkeypatch, handler)
    return seen


def test_pagination_walks_start_at_until_is_last(configured, monkeypatch):
    first = [dict(PROJECT_ENG, id=str(9000 + i), key="K{}".format(i)) for i in range(50)]
    seen = paged(
        monkeypatch,
        [
            {"isLast": False, "maxResults": 50, "values": first},
            {"isLast": True, "maxResults": 50, "values": [PROJECT_OPS]},
        ],
    )

    result = configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert len(result["projects"]) == 51
    assert seen == [0, 50], "the second page must be requested from where the first ended"
    assert result["truncated"] is False


def test_a_bare_array_is_accepted_as_an_unpaginated_answer(configured, monkeypatch):
    """The envelope is unverified, so a plain array must not be a hard failure."""
    patch_get(monkeypatch, FakeResponse(payload=[PROJECT_ENG, PROJECT_OPS]))

    result = configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert [p.key for p in result["projects"]] == ["ENG", "OPS"]
    assert result["truncated"] is False


def test_total_ends_the_walk_when_is_last_is_absent(configured, monkeypatch):
    """Jira may answer with `total` and no `isLast`; that must still terminate."""
    seen = paged(
        monkeypatch,
        [
            {"total": 2, "maxResults": 50, "values": [PROJECT_ENG]},
            {"total": 2, "maxResults": 50, "values": [PROJECT_OPS]},
        ],
    )

    result = configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert len(result["projects"]) == 2
    assert seen == [0, 1]


def test_a_short_page_ends_the_walk_when_no_signal_is_returned(configured, monkeypatch):
    """Neither `isLast` nor `total` came back, so a partial page is the only clue."""
    seen = paged(monkeypatch, [{"maxResults": 50, "values": [PROJECT_ENG]}])

    result = configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert len(result["projects"]) == 1
    assert seen == [0], "one short page is the end; asking again would loop"


def test_the_page_cap_reports_truncation_instead_of_dropping_rows(configured, monkeypatch):
    """
    A server that ignores startAt must not spin forever, and the user must be told
    the list is partial rather than shown a silent cap.
    """
    runaway = {
        "isLast": False,
        "maxResults": 50,
        "values": [dict(PROJECT_ENG, id=str(i), key="K{}".format(i)) for i in range(50)],
    }
    paged(monkeypatch, [runaway])

    result = configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert result["truncated"] is True
    assert len(result["projects"]) == JiraService.PAGE_SIZE * JiraService.MAX_PAGES
    assert any("incomplete" in note for note in result["notes"])


# --- A malformed body must never read as "no projects" ---------------------

@pytest.mark.parametrize(
    "payload,raises",
    [
        ({"projects": [PROJECT_ENG]}, False),  # collection under an unexpected key
        ("a string", False),
        (None, True),  # body could not be parsed at all
    ],
)
def test_an_unreadable_body_is_an_api_error_not_an_empty_list(
    configured, monkeypatch, payload, raises
):
    patch_get(monkeypatch, FakeResponse(payload=payload, raises=raises))

    with pytest.raises(ProviderAPIError):
        configured.list_projects(ACCESS_TOKEN, SITE.id)


def test_entries_without_an_id_or_key_do_not_look_like_zero_projects(configured, monkeypatch):
    """
    Returning [] here would read as a permissions problem and send the user to a
    Jira administrator for nothing.
    """
    patch_get(
        monkeypatch,
        FakeResponse(payload={"isLast": True, "values": [{"name": "Nameless"}]}),
    )

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert "id or a key" in str(excinfo.value)


def test_usable_projects_survive_alongside_an_unusable_one(configured, monkeypatch):
    patch_get(
        monkeypatch,
        FakeResponse(
            payload={"isLast": True, "values": [{"name": "Nameless"}, PROJECT_OPS, "junk"]}
        ),
    )

    result = configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert [p.key for p in result["projects"]] == ["OPS"]


# --- Errors reuse the shared provider hierarchy ---------------------------

@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ProviderTokenExpiredError),
        (403, ProviderConsentRequiredError),
        (429, ProviderAPIError),
        (500, ProviderAPIError),
        (503, ProviderAPIError),
    ],
)
def test_http_failures_map_onto_the_shared_error_hierarchy(
    configured, monkeypatch, status, expected
):
    patch_get(monkeypatch, FakeResponse(status_code=status, payload={"message": "nope"}))

    with pytest.raises(expected):
        configured.list_projects(ACCESS_TOKEN, SITE.id)


def test_a_network_failure_is_an_api_error(configured, monkeypatch):
    def boom(*a, **k):
        raise jira_service.requests.RequestException("connection reset")

    monkeypatch.setattr(jira_service.requests, "get", boom)

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.list_projects(ACCESS_TOKEN, SITE.id)

    assert "Network error" in str(excinfo.value)


def test_an_expired_token_is_refreshed_and_the_search_retried(configured, monkeypatch):
    """Proves project discovery reuses call_with_refresh rather than its own retry."""
    responses = [
        FakeResponse(status_code=401, payload={"message": "expired"}),
        FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG]}),
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
                "scope": FULL_SCOPES,
            }
        ),
    )

    result, refreshed = call_with_refresh(
        configured,
        jira_tokens(),
        lambda token: configured.list_projects(access_token=token, cloud_id=SITE.id),
    )

    assert [p.key for p in result["projects"]] == ["ENG"]
    # Atlassian disables the refresh token that was just used.
    assert refreshed.refresh_token == "rt-rotated-fake"


def test_an_authorization_failure_is_reported_and_nothing_is_cached(configured, monkeypatch):
    patch_get(monkeypatch, FakeResponse(status_code=403, payload={"message": "Forbidden"}))

    shown, _ = render_projects_panel(configured, jira_tokens(), monkeypatch)

    assert any("unauthorized" in text.lower() for text in shown["error"])
    assert main._skey("jira", "projects") not in st.session_state
    assert main._skey("jira", "project") not in st.session_state


def test_an_authorization_failure_mid_metadata_is_not_downgraded_to_a_note(
    configured, monkeypatch
):
    """
    A 403 on a field lookup needs the re-consent path, not a note buried in the
    results. Only ordinary API errors become notes.
    """

    def handler(url, params):
        if "/issuetypes/" in url:
            return FakeResponse(status_code=403, payload={"message": "Forbidden"})
        if url.endswith("/issuetypes"):
            return FakeResponse(payload={"isLast": True, "values": [TYPE_STORY]})
        return FakeResponse(payload={"isLast": True, "values": []})

    patch_routes(monkeypatch, handler)

    with pytest.raises(ProviderConsentRequiredError):
        configured.get_project_metadata(ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"])


# --- Issue types and required fields --------------------------------------

def test_issue_types_and_only_their_required_fields_are_parsed(configured, monkeypatch):
    patch_routes(
        monkeypatch,
        metadata_routes(
            [TYPE_STORY, TYPE_SUBTASK],
            {
                TYPE_STORY["id"]: [FIELD_SUMMARY, FIELD_LABELS],
                TYPE_SUBTASK["id"]: [FIELD_SUMMARY],
            },
        ),
    )

    metadata = configured.get_project_metadata(ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"])

    assert [t.name for t in metadata.issue_types] == ["Story", "Sub-task"]
    story = metadata.issue_types[0]
    assert story.description.startswith("A unit of work")
    # `labels` was returned but is not required, so it is not carried.
    assert [f.field_id for f in story.required_fields] == ["summary"]
    assert story.required_fields[0].schema_type == "string"
    assert metadata.truncated is False


def test_a_missing_project_is_refused_before_any_request(configured, monkeypatch):
    calls = patch_routes(monkeypatch, lambda url, params: FakeResponse(payload={"values": []}))

    with pytest.raises(ProviderAPIError) as excinfo:
        configured.get_project_metadata(ACCESS_TOKEN, SITE.id, "  ")

    assert "No Jira project is selected" in str(excinfo.value)
    assert calls == []


# --- ok / blocked / unknown validation states -----------------------------

def test_supported_and_defaulted_required_fields_validate_as_ok(configured, monkeypatch):
    """`summary` is one this app can fill; `reporter` is one Jira defaults itself."""
    patch_routes(
        monkeypatch,
        metadata_routes([TYPE_STORY], {TYPE_STORY["id"]: [FIELD_SUMMARY, FIELD_REPORTER]}),
    )

    metadata = configured.get_project_metadata(ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"])
    story = metadata.issue_types[0]

    assert story.validation_state == "ok"
    assert story.unsupported_required_fields == ()
    assert [t.name for t in metadata.plannable_issue_types] == ["Story"]


def test_an_unsupported_required_field_blocks_the_issue_type(configured, monkeypatch):
    patch_routes(
        monkeypatch,
        metadata_routes([TYPE_STORY], {TYPE_STORY["id"]: [FIELD_SUMMARY, FIELD_SQUAD]}),
    )

    metadata = configured.get_project_metadata(ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"])
    story = metadata.issue_types[0]

    assert story.validation_state == "blocked"
    assert [f.field_id for f in story.unsupported_required_fields] == ["customfield_10101"]
    assert metadata.plannable_issue_types == ()


def test_a_failed_field_lookup_is_unknown_rather_than_ok(configured, monkeypatch):
    """
    An issue type whose fields could not be read is unvalidated, not validated.
    Collapsing the two would report an unchecked type as safe to plan against.
    """
    patch_routes(
        monkeypatch,
        metadata_routes(
            [TYPE_STORY, TYPE_WIDGET],
            {TYPE_STORY["id"]: [FIELD_SUMMARY]},  # TYPE_WIDGET answers 500
        ),
    )

    metadata = configured.get_project_metadata(ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"])

    states = {t.name: t.validation_state for t in metadata.issue_types}
    assert states == {"Story": "ok", "Widget": "unknown"}
    # One awkward issue type must not cost the whole project, but it must be said.
    assert any("Widget" in note for note in metadata.notes)
    assert [t.name for t in metadata.plannable_issue_types] == ["Story"]


# --- No hardcoded hierarchy -----------------------------------------------

def test_hierarchy_is_reported_exactly_as_jira_stated_it(configured, monkeypatch):
    """
    PRD.md forbids assuming every project shares one hierarchy, so the discovered
    level and subtask flag are the whole answer and no level gets a name.
    """
    patch_routes(
        monkeypatch,
        metadata_routes(
            [TYPE_STORY, TYPE_SUBTASK, TYPE_WIDGET],
            {
                TYPE_STORY["id"]: [FIELD_SUMMARY],
                TYPE_SUBTASK["id"]: [FIELD_SUMMARY],
                TYPE_WIDGET["id"]: [FIELD_SUMMARY],
            },
        ),
    )

    metadata = configured.get_project_metadata(ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"])
    notes = {t.name: t.hierarchy_note for t in metadata.issue_types}

    assert notes["Story"] == "Jira hierarchy level 0, not a subtask type"
    assert notes["Sub-task"] == "Jira hierarchy level -1, subtask type"
    assert notes["Widget"] == "Jira hierarchy level 3, not a subtask type"
    for name in ("Epic", "Story", "Task"):
        assert name not in notes["Widget"], "level 3 must not be given a name"


def test_an_unreported_hierarchy_level_is_not_invented(configured, monkeypatch):
    bare = {"id": "10200", "name": "Undeclared"}
    patch_routes(monkeypatch, metadata_routes([bare], {bare["id"]: [FIELD_SUMMARY]}))

    issue_type = configured.get_project_metadata(
        ACCESS_TOKEN, SITE.id, PROJECT_ENG["id"]
    ).issue_types[0]

    assert issue_type.hierarchy_level is None
    assert issue_type.hierarchy_note == "not a subtask type"


def test_the_rendered_metadata_names_no_hierarchy_level(configured, monkeypatch):
    patch_routes(
        monkeypatch,
        metadata_routes([TYPE_WIDGET], {TYPE_WIDGET["id"]: [FIELD_SUMMARY]}),
    )

    shown, _ = render_projects_panel(
        configured,
        jira_tokens(),
        monkeypatch,
        clicked=("discover_jira_projects", "discover_jira_metadata"),
    )

    rendered = " ".join(text for texts in shown.values() for text in texts)
    assert "Jira hierarchy level 3" in rendered
    for invented in ("Epic", "Story", "Sub-task"):
        assert invented not in rendered


# --- Stale project and metadata state ------------------------------------

def test_changing_the_site_clears_projects_and_metadata(configured, monkeypatch):
    """
    A project list belongs to one site. Left in place it would offer projects that
    do not exist on the newly selected site.
    """
    patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG]}))
    render_projects_panel(configured, jira_tokens(), monkeypatch, site=SITE)
    assert st.session_state[main._skey("jira", "projects_site")] == SITE.id

    # Re-render for a different site with nothing pressed: the cache must be gone
    # rather than silently reused.
    shown, picks = render_projects_panel(
        configured, jira_tokens(), monkeypatch, site=OTHER_SITE, clicked=()
    )

    for suffix in ("projects", "projects_site", "project", "metadata", "metadata_for"):
        assert main._skey("jira", suffix) not in st.session_state, suffix
    assert picks == []
    assert any("No project has been requested yet" in text for text in shown["caption"])


def test_requerying_projects_drops_a_stale_selection_and_metadata(configured, monkeypatch):
    st.session_state[main._skey("jira", "project")] = JiraProject.from_api(PROJECT_OPS)
    st.session_state[main._skey("jira", "metadata")] = "stale"
    st.session_state[main._skey("jira", "metadata_for")] = (SITE.id, PROJECT_OPS["id"])
    st.session_state["select_jira_project"] = 1
    patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": [PROJECT_ENG]}))

    render_projects_panel(configured, jira_tokens(), monkeypatch)

    assert st.session_state[main._skey("jira", "project")].id == PROJECT_ENG["id"]
    assert main._skey("jira", "metadata") not in st.session_state
    assert "select_jira_project" not in st.session_state


def test_choosing_a_different_site_clears_projects_from_the_sites_panel(
    configured, monkeypatch
):
    """Re-querying sites invalidates everything below it, not just the selection."""
    st.session_state[main._skey("jira", "projects")] = {"projects": [], "notes": [], "truncated": False}
    st.session_state[main._skey("jira", "projects_site")] = SITE.id
    st.session_state[main._skey("jira", "metadata")] = "stale"
    patch_get(monkeypatch, FakeResponse(payload=[SITE_A]))

    from test_jira_sites import render_sites_panel

    render_sites_panel(configured, jira_tokens(), monkeypatch)

    for suffix in ("projects", "projects_site", "metadata"):
        assert main._skey("jira", suffix) not in st.session_state, suffix


def test_disconnect_clears_every_project_and_metadata_key(configured):
    st.session_state[main._skey("jira", "tokens")] = jira_tokens()
    for suffix in ("sites", "site", "projects", "projects_site", "project", "metadata", "metadata_for"):
        st.session_state[main._skey("jira", suffix)] = "anything"

    main._disconnect(configured)

    assert [key for key in st.session_state if key.startswith("jira__")] == []


# --- Missing scope and reconnect ------------------------------------------

@pytest.mark.parametrize(
    "granted,expected",
    [
        (FULL_SCOPES.split(), []),
        # A session authorized under JIRA-003 keeps the narrower grant.
        (["read:me", "read:jira-user", "offline_access"], ["read:jira-work"]),
        # Silence is not evidence: a provider that omits `scope` must not be told
        # to reconnect.
        ((), []),
    ],
)
def test_missing_scopes_reports_only_what_a_live_session_lacks(configured, granted, expected):
    assert configured.missing_scopes(granted) == expected


def test_zero_projects_explains_a_missing_scope_when_that_is_the_cause(configured, monkeypatch):
    """
    A session authorized before read:jira-work was requested can never see a
    project, so pointing at Jira permissions would send the user hunting a problem
    that does not exist.
    """
    patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": []}))

    shown, _ = render_projects_panel(
        configured,
        jira_tokens(scopes="read:me read:jira-user offline_access"),
        monkeypatch,
    )

    warning = " ".join(shown["warning"])
    assert JiraService.PROJECT_SCOPE in warning
    assert "connect again" in warning
    assert main._skey("jira", "project") not in st.session_state


def test_zero_projects_points_at_jira_permissions_when_the_scope_was_granted(
    configured, monkeypatch
):
    """Atlassian documents that a Jira account's own permissions still apply."""
    patch_get(monkeypatch, FakeResponse(payload={"isLast": True, "values": []}))

    shown, _ = render_projects_panel(configured, jira_tokens(), monkeypatch)

    warning = " ".join(shown["warning"])
    assert "Browse Projects" in warning
    assert JiraService.PROJECT_SCOPE not in warning
    assert not shown["success"]
