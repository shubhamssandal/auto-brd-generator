"""
JIRA-007 tests: creating the approved Jira issues.

This is the first ticket that writes to Jira, so ``test_jira_sites``'
``forbid_write_requests`` fixture is deliberately not imported here. Every POST is
mocked; no Atlassian app, no credentials and no network access are needed.

Three properties carry most of the weight, because they are where a duplicate or a
half-built hierarchy would come from:

* a create is sent only inside the branch a confirmation button was pressed in, never
  on a page load or a Streamlit rerun;
* a create is never re-sent -- ``call_with_refresh``'s 401 retry is safe for a GET and
  unsafe for a POST, so ``create_issue`` raises an authentication error on 401 rather
  than the expiry error that would trigger that retry;
* parents are created before the children that name them, and a child is sent the Jira
  key its parent was actually created as, not its plan-local key.

Issue types are JIRA-005's Initiative / Deliverable / Step, at levels 2 / 0 / -1.
Nothing in creation may key off Jira's default hierarchy names.
"""

import inspect
from dataclasses import replace

import pytest
import streamlit as st

import jira_processor
import jira_service
import main
from jira_models import CreatedIssue, JiraSite
from jira_processor import (
    build_work_plan,
    creation_order,
    issue_browse_url,
    issue_creation_payload,
)
from jira_service import JiraService
from providers.base import (
    ProviderAPIError,
    ProviderAuthenticationError,
    ProviderConsentRequiredError,
    ProviderTokenExpiredError,
)

# JIRA-005's BRD, metadata and issue-type builders, so a plan here is the same object
# the review step produces rather than a fixture shaped to suit creation.
from test_jira_work_plan import (
    CONTAINER,
    ITEM,
    PROJECT,
    SCOPE,
    SUBTASK,
    a_brd,
    a_metadata,
    issues_by_key,
)
from test_jira_sites import (  # noqa: F401 -- clean_session is an autouse fixture
    ACCESS_TOKEN,
    SITE_A,
    FakeResponse,
    clean_session,
    configured,
    tokens_for,
)

SITE = JiraSite.from_api(SITE_A)
METADATA = a_metadata(CONTAINER, ITEM, SUBTASK)
WRITE_SCOPES = "read:me read:jira-user read:jira-work write:jira-work offline_access"

# The plan a_brd() produces: one container, three requirements under it, and the action
# item under FR-1. Named here because the ordering tests are about these relationships.
ROOT = "BRD"
CHILD = "FR-1"
GRANDCHILD = "FR-1-A1"


def a_plan():
    """The generated plan for the standard BRD. Nothing in it exists in Jira."""
    return build_work_plan(a_brd(), PROJECT, METADATA)


def with_issue(plan, plan_key, **changes):
    """The same plan with one issue changed, still frozen."""
    return replace(
        plan,
        issues=tuple(
            replace(issue, **changes) if issue.plan_key == plan_key else issue
            for issue in plan.issues
        ),
    )


def tokens():
    return tokens_for(scopes=WRITE_SCOPES)


class RecordingPost:
    """Records each POST and answers from a queue of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("more POSTs were sent than this test allowed")
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def fields_sent(self, index):
        return self.calls[index]["kwargs"]["json"]["fields"]


def created_ok(key, issue_id="10100"):
    """A create-issue success body: Jira returns id, key and self."""
    return FakeResponse(
        status_code=201, payload={"id": issue_id, "key": key, "self": "https://x"}
    )


def patch_post(monkeypatch, *responses):
    recorder = RecordingPost(responses)
    monkeypatch.setattr(jira_service.requests, "post", recorder)
    return recorder


def forbid_post(monkeypatch, why):
    """This ticket's own version of ``forbid_write_requests``, for the paths that must
    still not write."""
    monkeypatch.setattr(jira_service.requests, "post", lambda *a, **k: pytest.fail(why))


def create(service, payload):
    """One create call, so the error tests do not each repeat the argument list."""
    return service.create_issue(
        access_token=ACCESS_TOKEN, cloud_id=SITE.id, payload=payload
    )


def a_payload(plan_key=CHILD, parent_key=""):
    return issue_creation_payload(
        issues_by_key(a_plan())[plan_key], PROJECT.api_identifier, parent_key
    )


def paragraphs(description):
    return [block["content"][0]["text"] for block in description["content"]]


def render_creation_panel(
    monkeypatch, service, clicked=(), plan=None, metadata=METADATA, scopes=WRITE_SCOPES
):
    """
    Render the creation panel and capture what the user would see.

    ``clicked`` names the pressed buttons by widget key, which is what makes "nothing is
    created unless a button was pressed" testable: with ``clicked=()`` the panel renders
    exactly as it does on a page load or a rerun.
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
    monkeypatch.setattr(main.st, "rerun", lambda: None)

    if plan is not None:
        st.session_state[main._skey("jira", "plan")] = plan
        st.session_state[main._skey("jira", "plan_for")] = SCOPE

    main._render_jira_creation_panel(
        service, tokens_for(scopes=scopes), SITE, PROJECT, metadata
    )
    return shown


# --- The documented request ------------------------------------------------

def test_posts_the_documented_endpoint_headers_and_body(configured, monkeypatch):
    """
    Method, URL, headers and body against Atlassian's 3LO contract: the site-scoped
    ``/ex/jira/{cloudid}`` base, a bearer token, and the issue under ``fields``.
    """
    post = patch_post(monkeypatch, created_ok("ENG-1"))

    body = create(configured, a_payload())

    assert body["key"] == "ENG-1"
    call = post.calls[0]
    assert call["url"] == "https://api.atlassian.com/ex/jira/{}/rest/api/3/issue".format(
        SITE.id
    )
    assert call["kwargs"]["headers"] == {
        "Authorization": "Bearer {}".format(ACCESS_TOKEN),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    assert call["kwargs"]["timeout"] == JiraService.TIMEOUT_SECONDS
    fields = post.fields_sent(0)
    assert fields["project"] == {"id": PROJECT.api_identifier}
    assert fields["issuetype"] == {"id": ITEM.id}
    assert fields["summary"] == issues_by_key(a_plan())[CHILD].summary


def test_the_description_is_sent_as_atlassian_document_format():
    """v3 takes rich text as ADF, not as a string."""
    description = a_payload()["fields"]["description"]

    assert description["type"] == "doc" and description["version"] == 1
    assert {block["type"] for block in description["content"]} == {"paragraph"}
    assert any("accept card payments" in text for text in paragraphs(description))


def test_acceptance_criteria_travel_in_the_description_not_an_invented_field():
    """
    A Jira create screen has no acceptance-criteria field of its own, and writing to a
    custom field this app never discovered would be inventing one.
    """
    assert issues_by_key(a_plan())[CHILD].acceptance_criteria
    fields = a_payload()["fields"]

    assert set(fields) <= {"project", "issuetype", "summary", "description", "parent"}
    assert any(
        "card payment is accepted end to end" in text
        for text in paragraphs(fields["description"])
    )


@pytest.mark.parametrize(
    "identifier, expected",
    [("10001", {"id": "10001"}), ("ENG", {"key": "ENG"})],
    ids=["numeric-id", "project-key"],
)
def test_the_project_is_addressed_by_id_or_key_as_given(identifier, expected):
    issue = issues_by_key(a_plan())[CHILD]

    assert issue_creation_payload(issue, identifier)["fields"]["project"] == expected


def test_a_parent_is_sent_only_when_there_is_one():
    assert "parent" not in a_payload(ROOT)["fields"]
    assert a_payload(CHILD, "ENG-100")["fields"]["parent"] == {"key": "ENG-100"}


# --- Ordering and parent/child --------------------------------------------

@pytest.mark.parametrize("reverse", [False, True], ids=["plan-order", "reversed-plan"])
def test_parents_come_before_the_children_that_name_them(reverse):
    """
    Order is derived, not trusted: a review edit or a delete can leave a child earlier
    in the tuple than its parent.
    """
    plan = a_plan()
    if reverse:
        plan = replace(plan, issues=tuple(reversed(plan.issues)))

    ordered = [issue.plan_key for issue in creation_order(plan)]

    assert sorted(ordered) == sorted(issue.plan_key for issue in plan.issues)
    assert ordered.index(ROOT) < ordered.index(CHILD) < ordered.index(GRANDCHILD)


def test_a_deselected_issue_is_not_created_and_takes_its_children_with_it():
    """Its child would otherwise name a parent that is not being created."""
    ordered = [
        issue.plan_key
        for issue in creation_order(with_issue(a_plan(), CHILD, selected=False))
    ]

    assert CHILD not in ordered and GRANDCHILD not in ordered
    assert ROOT in ordered and "FR-2" in ordered


def test_a_child_is_sent_the_key_its_parent_was_actually_created_as(configured, monkeypatch):
    """
    The plan holds plan-local keys. Sending one as ``parent`` would name an issue that
    does not exist in Jira, so the key Jira returned has to be threaded through.
    """
    post = patch_post(
        monkeypatch, *[created_ok("ENG-10{}".format(n), str(n)) for n in range(5)]
    )

    results = main._create_selected_issues(
        configured, tokens(), SITE.id, PROJECT, a_plan()
    )

    assert all(record.succeeded for record in results)
    assert [record.plan_key for record in results] == [
        issue.plan_key for issue in creation_order(a_plan())
    ]
    parent_sent = {
        record.plan_key: post.fields_sent(index).get("parent")
        for index, record in enumerate(results)
    }
    created_as = {record.plan_key: record.issue_key for record in results}
    assert parent_sent[ROOT] is None
    assert parent_sent[CHILD] == {"key": created_as[ROOT]}
    assert parent_sent[GRANDCHILD] == {"key": created_as[CHILD]}


# --- Failures are reported honestly ---------------------------------------

@pytest.mark.parametrize(
    "response, expected_error, fragment",
    [
        (
            FakeResponse(401, {"message": "unauthorized"}),
            ProviderAuthenticationError,
            "not created",
        ),
        (
            FakeResponse(403, {"message": "forbidden"}),
            ProviderConsentRequiredError,
            "write:jira-work",
        ),
        (FakeResponse(429, {"message": "too many"}), ProviderAPIError, "not created"),
        (FakeResponse(400, {"message": "field required"}), ProviderAPIError, "400"),
        (FakeResponse(500, text="upstream boom"), ProviderAPIError, "500"),
        (FakeResponse(201, {"self": "https://x"}), ProviderAPIError, "no issue key"),
    ],
    ids=["401", "403", "429", "400", "500", "201-without-a-key"],
)
def test_a_failed_create_is_reported_as_the_right_error(
    configured, monkeypatch, response, expected_error, fragment
):
    patch_post(monkeypatch, response)

    with pytest.raises(expected_error) as caught:
        create(configured, a_payload())

    assert fragment in str(caught.value)


def test_a_401_is_not_the_expiry_error_that_would_retry_the_create(configured, monkeypatch):
    """
    The safety property of this ticket. ``call_with_refresh`` reacts to
    ``ProviderTokenExpiredError`` by re-running the operation, which for a
    non-idempotent POST could create a second issue.
    """
    patch_post(monkeypatch, FakeResponse(401, {"message": "expired"}))

    with pytest.raises(ProviderAuthenticationError) as caught:
        create(configured, a_payload())

    assert not isinstance(caught.value, ProviderTokenExpiredError)


def test_a_network_error_is_reported_as_an_unknown_outcome_not_a_failure(
    configured, monkeypatch
):
    """The request may have reached Jira, so reporting "it failed" would be a guess."""
    patch_post(monkeypatch, jira_service.requests.RequestException("connection reset"))

    with pytest.raises(ProviderAPIError) as caught:
        create(configured, a_payload())

    message = str(caught.value)
    assert "unknown" in message and "Check Jira" in message


def test_a_partial_failure_stops_and_still_reports_what_was_created(configured, monkeypatch):
    """
    Continuing past a failure would create children whose parent is missing. The
    successes must still be reported by their real Jira keys.
    """
    post = patch_post(
        monkeypatch,
        created_ok("ENG-100", "1"),
        created_ok("ENG-101", "2"),
        FakeResponse(400, {"message": "field 'summary' is required"}),
    )

    results = main._create_selected_issues(
        configured, tokens(), SITE.id, PROJECT, a_plan()
    )

    assert [record.succeeded for record in results] == [True, True, False]
    assert [record.issue_key for record in results[:2]] == ["ENG-100", "ENG-101"]
    assert "summary" in results[-1].error
    assert len(post.calls) == 3, "nothing may be attempted after a failure"


def test_the_results_keep_created_and_failed_apart(monkeypatch):
    shown = {key: [] for key in ("success", "error", "write", "caption", "markdown")}
    for widget in shown:
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, _w=widget, **k: shown[_w].append(str(text))
        )

    main._render_created_results(
        (
            CreatedIssue(plan_key=ROOT, issue_key="ENG-100", issue_id="1"),
            CreatedIssue(plan_key=CHILD, error="Jira returned HTTP 400: bad field"),
        )
    )

    assert any("Created 1 issue" in text for text in shown["success"])
    assert any("1 issue(s) were not created" in text for text in shown["error"])
    assert "ENG-100" in " ".join(shown["markdown"])
    rendered = " ".join(shown["write"])
    assert CHILD in rendered and "bad field" in rendered
    assert any("retried automatically" in text for text in shown["caption"])


# --- JIRA-008: which planned item became which Jira issue ------------------

@pytest.mark.parametrize(
    "site_url, issue_key, expected",
    [
        ("https://acme-eng.atlassian.net", "ENG-100",
         "https://acme-eng.atlassian.net/browse/ENG-100"),
        ("https://acme-eng.atlassian.net/", "ENG-100",
         "https://acme-eng.atlassian.net/browse/ENG-100"),
        ("", "ENG-100", ""),
        ("https://acme-eng.atlassian.net", "", ""),
    ],
)
def test_the_issue_link_is_built_from_the_selected_site(site_url, issue_key, expected):
    assert issue_browse_url(site_url, issue_key) == expected


def test_each_result_carries_the_plan_item_and_its_brd_sources(configured, monkeypatch):
    """
    The mapping is recorded as the run happens, not looked up in the plan afterwards:
    the reviewer can replace the plan in the same session.
    """
    patch_post(monkeypatch, created_ok("ENG-100", "1"), created_ok("ENG-101", "2"),
               created_ok("ENG-102", "3"), created_ok("ENG-103", "4"))
    plan = a_plan()
    planned = issues_by_key(plan)

    results = main._create_selected_issues(configured, tokens(), SITE.id, PROJECT, plan)

    by_plan_key = {record.plan_key: record for record in results}
    assert by_plan_key[CHILD].summary == planned[CHILD].summary
    assert by_plan_key[CHILD].source_requirement_ids == planned[CHILD].requirement_ids
    assert CHILD in by_plan_key[CHILD].source_requirement_ids
    assert by_plan_key[GRANDCHILD].source_action_item_ids == tuple(
        planned[GRANDCHILD].source_action_item_ids
    )


def test_a_partial_failure_maps_both_outcomes_back_to_their_plan_items(
    configured, monkeypatch
):
    patch_post(
        monkeypatch,
        created_ok("ENG-100", "1"),
        created_ok("ENG-101", "2"),
        FakeResponse(400, {"message": "field 'summary' is required"}),
    )
    plan = a_plan()

    results = main._create_selected_issues(configured, tokens(), SITE.id, PROJECT, plan)
    shown = {key: [] for key in ("success", "error", "write", "caption", "markdown")}
    for widget in shown:
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, _w=widget, **k: shown[_w].append(str(text))
        )
    main._render_created_results(results, SITE.url)

    created, failed = results[1], results[-1]
    assert created.succeeded and not failed.succeeded
    assert failed.summary and failed.source_requirement_ids

    link = " ".join(shown["markdown"])
    assert "[ENG-101]({}/browse/ENG-101)".format(SITE.url) in link
    assert created.summary in link
    assert "from requirement(s) {}".format(", ".join(created.source_requirement_ids)) in link

    not_created = " ".join(shown["write"])
    assert failed.plan_key in not_created and "summary" in not_created
    assert failed.summary in not_created
    assert "ENG-101" not in not_created, "a failure line must not read as a success"


def test_the_mapping_survives_the_reruns_that_follow_a_run(configured, monkeypatch):
    """Stored results are re-rendered with their links, so the trail stays available."""
    st.session_state[main._skey("jira", "created")] = (
        CreatedIssue(
            plan_key=ROOT,
            issue_key="ENG-100",
            issue_id="1",
            summary="Ordering programme",
            source_requirement_ids=("FR-1",),
            source_action_item_ids=("AI-1",),
        ),
    )
    forbid_post(monkeypatch, "a completed run must not create anything again")

    shown = render_creation_panel(monkeypatch, configured, clicked=(), plan=a_plan())

    line = " ".join(shown["markdown"])
    assert "[ENG-100]({}/browse/ENG-100)".format(SITE.url) in line
    assert "Ordering programme" in line
    assert "requirement(s) FR-1" in line and "action item(s) AI-1" in line


# --- Nothing is created without an explicit, confirmed action -------------

def test_a_page_load_or_rerun_sends_no_request(configured, monkeypatch):
    forbid_post(monkeypatch, "rendering the panel must not create anything")

    shown = render_creation_panel(monkeypatch, configured, clicked=(), plan=a_plan())

    assert main._skey("jira", "created") not in st.session_state
    assert any("would be created" in text for text in shown["write"])


def test_the_first_click_only_asks_for_confirmation(configured, monkeypatch):
    forbid_post(monkeypatch, "the first click must not create anything")

    render_creation_panel(
        monkeypatch, configured, clicked=("request_jira_creation",), plan=a_plan()
    )

    assert st.session_state[main._skey("jira", "confirm_create")] is True
    assert main._skey("jira", "created") not in st.session_state


def test_the_confirmation_summarizes_count_target_and_issue_types(configured, monkeypatch):
    shown = render_creation_panel(monkeypatch, configured, clicked=(), plan=a_plan())

    summary = " ".join(shown["write"])
    assert "5 issue(s)" in summary
    assert PROJECT.display_label in summary
    for name in (CONTAINER.name, ITEM.name, SUBTASK.name):
        assert name in summary
    # No hierarchy name is invented anywhere in the creation step.
    rendered = " ".join(text for texts in shown.values() for text in texts)
    for invented in ("Epic", "Story", "Sub-task"):
        assert invented not in rendered


def test_confirming_creates_and_stores_the_results(configured, monkeypatch):
    post = patch_post(
        monkeypatch, *[created_ok("ENG-10{}".format(n), str(n)) for n in range(5)]
    )
    st.session_state[main._skey("jira", "confirm_create")] = True

    shown = render_creation_panel(
        monkeypatch, configured, clicked=("confirm_jira_creation",), plan=a_plan()
    )

    assert len(post.calls) == 5
    stored = st.session_state[main._skey("jira", "created")]
    assert [record.issue_key for record in stored] == [
        "ENG-10{}".format(n) for n in range(5)
    ]
    assert any("Created 5 issue(s)" in text for text in shown["success"])
    # The confirmation and the in-flight guard are both consumed, so a later rerun
    # neither sits armed nor looks stuck.
    assert main._skey("jira", "confirm_create") not in st.session_state
    assert main._skey("jira", "creating") not in st.session_state


def test_a_rerun_after_a_run_shows_the_results_and_creates_nothing_more(
    configured, monkeypatch
):
    """
    Stored results short-circuit the panel, so the rerun that follows a creation -- or a
    second click on a stale page -- cannot start a second run.
    """
    st.session_state[main._skey("jira", "created")] = (
        CreatedIssue(plan_key=ROOT, issue_key="ENG-100", issue_id="1"),
    )
    forbid_post(monkeypatch, "a completed run must not create anything again")

    shown = render_creation_panel(
        monkeypatch,
        configured,
        clicked=("confirm_jira_creation", "request_jira_creation"),
        plan=a_plan(),
    )

    assert any("Created 1 issue" in text for text in shown["success"])


def test_an_in_flight_run_refuses_to_start_a_second_one(configured, monkeypatch):
    st.session_state[main._skey("jira", "confirm_create")] = True
    st.session_state[main._skey("jira", "creating")] = True
    forbid_post(monkeypatch, "a second run must not start while one is in flight")

    shown = render_creation_panel(
        monkeypatch, configured, clicked=("confirm_jira_creation",), plan=a_plan()
    )

    assert any("already in progress" in text for text in shown["info"])


def test_an_invalid_plan_is_not_creatable(configured, monkeypatch):
    """Validation runs immediately before the write, not only at review time."""
    forbid_post(monkeypatch, "an invalid plan must not be created")

    shown = render_creation_panel(
        monkeypatch,
        configured,
        clicked=("request_jira_creation", "confirm_jira_creation"),
        plan=with_issue(a_plan(), CHILD, summary="   "),
    )

    assert any("cannot be created yet" in text for text in shown["warning"])


def test_a_plan_with_nothing_selected_offers_no_creation(configured, monkeypatch):
    """Blocked by validation, which reports the empty selection as a problem."""
    forbid_post(monkeypatch, "nothing selected must not create anything")
    plan = a_plan()
    for issue in plan.issues:
        plan = with_issue(plan, issue.plan_key, selected=False)

    shown = render_creation_panel(
        monkeypatch,
        configured,
        clicked=("request_jira_creation", "confirm_jira_creation"),
        plan=plan,
    )

    assert any("cannot be created yet" in text for text in shown["warning"])
    assert main._skey("jira", "created") not in st.session_state


def a_cycle(plan, *plan_keys):
    """The plan with two issues made each other's parent, so neither can be ordered."""
    first, second = plan_keys
    return with_issue(
        with_issue(plan, first, parent_plan_key=second), second, parent_plan_key=first
    )


def test_an_unorderable_plan_is_not_creatable_and_is_not_misdiagnosed(
    configured, monkeypatch
):
    """
    A parent cycle passes validation but cannot be ordered, so this is the one path
    where the panel is the last check. It must not report it as an empty selection.
    """
    forbid_post(monkeypatch, "an unorderable plan must not be created")
    plan = a_plan()
    cyclic = a_cycle(replace(plan, issues=plan.issues[:2]), ROOT, CHILD)
    assert not creation_order(cyclic)

    shown = render_creation_panel(
        monkeypatch,
        configured,
        clicked=("request_jira_creation", "confirm_jira_creation"),
        plan=cyclic,
    )

    warning = " ".join(shown["warning"])
    assert "Nothing can be created" in warning
    assert "parent relationship" in warning
    assert main._skey("jira", "created") not in st.session_state


def test_selected_issues_that_cannot_be_ordered_are_named_before_confirming(
    configured, monkeypatch
):
    """
    A selected issue left out of the run has to be stated up front. Discovering it from
    a result list shorter than the plan would read as if it had been created.
    """
    forbid_post(monkeypatch, "the summary must not create anything")
    plan = a_plan()
    unorderable = a_cycle(
        replace(
            plan,
            issues=plan.issues
            + (
                replace(plan.issues[1], plan_key="X"),
                replace(plan.issues[1], plan_key="Y"),
            ),
        ),
        "X",
        "Y",
    )

    shown = render_creation_panel(monkeypatch, configured, clicked=(), plan=unorderable)

    warning = " ".join(shown["warning"])
    assert "2 selected issue(s) cannot be placed" in warning
    assert "X" in warning and "Y" in warning
    # The count offered for creation is the number that will actually be created.
    assert "5 issue(s)" in " ".join(shown["write"])


# --- Scope and boundaries -------------------------------------------------

def test_a_session_without_the_write_scope_is_told_before_it_tries(configured, monkeypatch):
    """
    Atlassian grants scopes at consent time and a refresh cannot widen them, so a
    session that connected before JIRA-007 can read and plan but cannot create. Saying
    so before offering the button beats a 403 partway through a run.
    """
    forbid_post(monkeypatch, "a session without the write scope must not try to create")

    shown = render_creation_panel(
        monkeypatch,
        configured,
        clicked=("request_jira_creation", "confirm_jira_creation"),
        plan=a_plan(),
        scopes="read:me read:jira-user read:jira-work offline_access",
    )

    warning = " ".join(shown["warning"])
    assert "write:jira-work" in warning and "cannot create issues" in warning
    assert main._skey("jira", "created") not in st.session_state


def test_an_unreported_scope_list_does_not_block_creation(configured, monkeypatch):
    """
    Silence is not evidence: a token response that omits ``scope`` must not be treated
    as a session lacking the write scope, which is how the read steps already read it.
    """
    post = patch_post(
        monkeypatch, *[created_ok("ENG-10{}".format(n), str(n)) for n in range(5)]
    )
    st.session_state[main._skey("jira", "confirm_create")] = True

    render_creation_panel(
        monkeypatch,
        configured,
        clicked=("confirm_jira_creation",),
        plan=a_plan(),
        scopes="",
    )

    assert len(post.calls) == 5


def test_the_write_scope_is_requested_and_is_the_only_write_scope():
    """
    The scope set itself is asserted in ``test_jira_oauth``. What matters here is that
    the constant the creation path names is the one that was actually requested.
    """
    assert JiraService.WRITE_SCOPE == "write:jira-work"
    assert JiraService.WRITE_SCOPE in JiraService.SCOPES


def test_the_new_pure_functions_cannot_reach_jira():
    """
    ``jira_processor`` builds the payload and the order but must not be able to send
    either: every HTTP call belongs to ``jira_service``. (That the module imports no
    HTTP client at all is asserted in ``test_jira_work_plan``.)
    """
    for name in ("issue_creation_payload", "creation_order"):
        parameters = inspect.signature(getattr(jira_processor, name)).parameters
        assert not {"access_token", "tokens", "service"} & set(parameters)


def test_a_new_brd_drops_a_previous_run_s_results():
    """
    Results describe one plan against one target. Inheriting them would report issues
    as created for a proposal they were never created from.
    """
    for suffix in ("created", "creating", "confirm_create"):
        assert suffix in main._JIRA_PROJECT_SUFFIXES

    st.session_state[main._skey("jira", "created")] = (
        CreatedIssue(plan_key=ROOT, issue_key="ENG-1", issue_id="1"),
    )
    st.session_state[main._skey("jira", "confirm_create")] = True

    main._store_brd(a_brd(project_title="A different meeting"))

    assert main._skey("jira", "created") not in st.session_state
    assert main._skey("jira", "confirm_create") not in st.session_state
