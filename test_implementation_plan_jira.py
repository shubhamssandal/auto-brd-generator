"""
Phase 5 of the delivery lifecycle: an approved implementation plan onto Jira issues.

Seven properties are pinned here, and they are the ones that make this stage safe to
point at a real tracker.

An **approved** plan is the gate -- a draft plan creates nothing. The **project's own
hierarchy** decides the issue types, so a project calling its rungs
Initiative/Feature/Work Item is served as well as one calling them Epic/Story/Subtask,
and a hierarchy Jira cannot represent is *reported* rather than flattened onto the
wrong parent. The **plan's own identifiers** are the issue keys' counterparts, so the
mapping is stable across reruns and edits. **Creation is idempotent**: Jira has no
idempotency key on its create endpoint, so an item that already has an issue is never
sent again, and a retry after a partial failure finishes the run instead of doubling
it. A **write needs an explicit confirmation** and never happens on a page load.
**Status is read from creation evidence** this app recorded, never from a Jira workflow
read -- there is no such read here, and the one that used to exist was removed with the
requirement-drift feature it served. And nothing about Jira is allowed to reach back
into the **BRD, PRD, architecture or plan**.

Everything below the `--- Session state ---` marker touches ``main`` and therefore
Streamlit; everything above it is pure and needs neither.
"""

import pytest

import implementation_plan_jira as delivery
import implementation_plan_models as plan_models
from implementation_plan_jira import (
    MAX_DELIVERY_ISSUES,
    DeliveryLink,
    DeliveryMapping,
    delivery_progress,
    known_issue_keys,
    map_plan_to_work_plan,
    pending_plan_keys,
    record_created_issues,
)
from implementation_plan_models import Epic, ImplementationPlan, Story, TechnicalTask
from jira_models import CreatedIssue, JiraIssueType, JiraProject, JiraProjectMetadata
from jira_processor import creation_order, issue_description_text, validate_work_plan

PROJECT = JiraProject(id="10", key="ENG", name="Engineering")


def _types(*specs) -> tuple:
    """Issue types as Jira reports them: (id, name, subtask, hierarchy_level)."""
    return tuple(
        JiraIssueType(
            id=type_id,
            name=name,
            subtask=subtask,
            hierarchy_level=level,
            fields_known=True,
        )
        for type_id, name, subtask, level in specs
    )


def _metadata(*specs) -> JiraProjectMetadata:
    """Metadata read for PROJECT. Defaults to a conventional three-rung project."""
    if not specs:
        specs = (
            ("1", "Epic", False, 1),
            ("2", "Story", False, 0),
            ("3", "Task", False, 0),
            ("4", "Subtask", True, -1),
        )
    return JiraProjectMetadata(
        project_identifier=PROJECT.api_identifier, issue_types=_types(*specs)
    )


def _plan(epics=True, tasks=True) -> ImplementationPlan:
    """A two-epic plan with a story each and one technical task per story."""
    stories = (
        Story(
            story_id="S1",
            title="First story",
            epic_id="E1" if epics else "",
            user_story="As a user, I want the first thing.",
            acceptance_criteria=("Given a, when b, then c",),
            feature_ids=("F-1",),
            component_ids=("C-1",),
            priority="High",
            tasks=(
                TechnicalTask(task_id="T1", title="Backend work", component_ids=("C-1",)),
            )
            if tasks
            else (),
        ),
        Story(
            story_id="S2",
            title="Second story",
            epic_id="E2" if epics else "",
            user_story="As a user, I want the second thing.",
            feature_ids=("F-2",),
            component_ids=("C-2",),
            tasks=(
                TechnicalTask(
                    task_id="T2",
                    title="Web work",
                    work_type=plan_models.SUBTASK,
                    component_ids=("C-2",),
                ),
            )
            if tasks
            else (),
        ),
    )
    return ImplementationPlan(
        project_title="Any product",
        overview="An overview.",
        epics=(
            Epic(epic_id="E1", name="First epic", goal="A goal", feature_ids=("F-1",)),
            Epic(epic_id="E2", name="Second epic", feature_ids=("F-2",)),
        )
        if epics
        else (),
        stories=stories,
        source_prd_title="The PRD",
        source_architecture_title="The architecture",
    )


def _mapped(plan=None, metadata=None):
    return map_plan_to_work_plan(plan or _plan(), metadata or _metadata(), PROJECT)


def _ok(plan_key: str, issue_key: str, summary: str = "") -> CreatedIssue:
    return CreatedIssue(
        plan_key=plan_key, issue_key=issue_key, issue_id=issue_key, summary=summary
    )


def _failed(plan_key: str, error: str = "Jira returned HTTP 400") -> CreatedIssue:
    return CreatedIssue(plan_key=plan_key, error=error)


# --- Mapping onto the project's own hierarchy -----------------------------


def test_the_plan_becomes_issues_typed_by_the_project_not_by_this_app():
    work_plan = _mapped()

    by_key = {issue.plan_key: issue for issue in work_plan.issues}
    # Epic -> the top rung, story -> the next, technical task -> the one below.
    assert by_key["E1"].issue_type_name == "Epic"
    assert by_key["S1"].issue_type_name == "Story"
    assert by_key["T1"].issue_type_name == "Subtask"
    # The hierarchy level carried is Jira's own number for the type, which is what
    # validate_work_plan checks the issue against.
    assert by_key["E1"].hierarchy_level == 1
    assert by_key["S1"].hierarchy_level == 0
    assert by_key["T1"].hierarchy_level == -1
    assert work_plan.project_identifier == PROJECT.api_identifier
    assert validate_work_plan(work_plan, _metadata(), PROJECT) == ()


def test_the_epic_story_task_relationships_survive_the_mapping():
    work_plan = _mapped()

    by_key = {issue.plan_key: issue for issue in work_plan.issues}
    assert by_key["E1"].parent_plan_key == ""
    assert by_key["S1"].parent_plan_key == "E1"
    assert by_key["T1"].parent_plan_key == "S1"
    assert by_key["S2"].parent_plan_key == "E2"
    assert by_key["T2"].parent_plan_key == "S2"
    # And creation order is parents first, which is a correctness requirement of the
    # write rather than a display choice.
    order = [issue.plan_key for issue in creation_order(work_plan)]
    assert order.index("E1") < order.index("S1") < order.index("T1")


def test_a_project_with_its_own_vocabulary_is_served_by_its_own_types():
    """No project is required to call its levels Epic, Story and Task."""
    metadata = _metadata(
        ("1", "Initiative", False, 2),
        ("2", "Feature", False, 1),
        ("3", "Work Item", False, 0),
    )

    work_plan = map_plan_to_work_plan(_plan(), metadata, PROJECT)

    by_key = {issue.plan_key: issue for issue in work_plan.issues}
    assert by_key["E1"].issue_type_name == "Initiative"
    assert by_key["S1"].issue_type_name == "Feature"
    assert by_key["T1"].issue_type_name == "Work Item"
    assert all(issue.selected for issue in work_plan.issues)
    # The substitution is stated rather than silent.
    assert any("Initiative" in note for note in work_plan.notes)
    assert validate_work_plan(work_plan, metadata, PROJECT) == ()


def test_a_hierarchy_the_project_cannot_represent_is_reported_not_flattened():
    """
    The rule this stage exists to keep: a task created as a sibling of its own story
    would misstate the plan, so it is left out and said out loud instead.
    """
    metadata = _metadata(("1", "Epic", False, 1), ("2", "Story", False, 0))

    work_plan = map_plan_to_work_plan(_plan(), metadata, PROJECT)

    by_key = {issue.plan_key: issue for issue in work_plan.issues}
    assert by_key["E1"].selected is True
    assert by_key["S1"].selected is True
    # The tasks are present and visible, but not selected and not typed.
    assert by_key["T1"].selected is False
    assert by_key["T2"].selected is False
    assert by_key["T1"].issue_type_id == ""
    assert any(
        "2 hierarchy rung(s)" in note and "needs 3" in note for note in work_plan.notes
    ), work_plan.notes
    # What does fit is still creatable, and nothing was reparented.
    assert validate_work_plan(work_plan, metadata, PROJECT) == ()
    assert [issue.plan_key for issue in creation_order(work_plan)] == ["E1", "E2", "S1", "S2"]


def test_a_plan_without_epics_leaves_the_top_rung_unused_rather_than_miscasting_stories():
    """A story is a story. Promoting it to the epic rung would misreport the plan."""
    work_plan = map_plan_to_work_plan(_plan(epics=False), _metadata(), PROJECT)

    by_key = {issue.plan_key: issue for issue in work_plan.issues}
    assert by_key["S1"].issue_type_name == "Story"
    assert by_key["S1"].parent_plan_key == ""
    assert by_key["S1"].selected is True
    assert "Epic" not in work_plan.issue_type_names
    assert validate_work_plan(work_plan, _metadata(), PROJECT) == ()


def test_a_story_whose_epic_could_not_be_created_is_created_without_a_parent():
    """Real work is not dropped because its container did not fit; the link is noted."""
    plan = ImplementationPlan(
        epics=(),
        stories=(Story(story_id="S9", title="Orphan", epic_id="E9", feature_ids=("F-1",)),),
    )

    work_plan = map_plan_to_work_plan(plan, _metadata(), PROJECT)

    story = work_plan.issues[0]
    assert story.plan_key == "S9"
    assert story.parent_plan_key == ""
    assert story.selected is True
    assert any("E9" in note for note in work_plan.notes)
    # The plan's own epic id is still readable on the issue.
    assert "E9" in issue_description_text(story) or "S9" in issue_description_text(story)


@pytest.mark.parametrize(
    "metadata, expected_note",
    [
        (JiraProjectMetadata(project_identifier="10"), "no issue type"),
        (
            JiraProjectMetadata(
                project_identifier="10",
                issue_types=_types(("9", "Anything", False, 0)),
            ),
            "hierarchy rung",
        ),
    ],
    ids=["nothing-plannable", "one-rung-only"],
)
def test_a_project_that_cannot_hold_the_plan_says_so(metadata, expected_note):
    work_plan = map_plan_to_work_plan(_plan(), metadata, PROJECT)

    assert any(expected_note in note for note in work_plan.notes), work_plan.notes


def test_no_approved_plan_produces_no_issues_and_says_why():
    for empty in (None, ImplementationPlan()):
        work_plan = map_plan_to_work_plan(empty, _metadata(), PROJECT)

        assert work_plan.issues == ()
        assert any("approve" in note.lower() for note in work_plan.notes)


def test_the_run_is_bounded_and_the_excess_stays_visible():
    """An unbounded run is an unbounded number of writes."""
    stories = tuple(
        Story(story_id="S{}".format(n), title="Story {}".format(n), feature_ids=("F-1",))
        for n in range(MAX_DELIVERY_ISSUES + 5)
    )

    work_plan = map_plan_to_work_plan(
        ImplementationPlan(stories=stories), _metadata(), PROJECT
    )

    selected = [issue for issue in work_plan.issues if issue.selected]
    assert len(selected) == MAX_DELIVERY_ISSUES
    # The remainder is kept in the proposal, so a later run can create it.
    assert len(work_plan.issues) == MAX_DELIVERY_ISSUES + 5


# --- Traceability -------------------------------------------------------


def test_every_issue_carries_its_plan_id_and_its_prd_and_architecture_references():
    """
    One-way traceability. The description is what reaches Jira, so a reviewer reading
    the issue there can get back to the artifact it came from.
    """
    work_plan = _mapped()
    by_key = {issue.plan_key: issue for issue in work_plan.issues}

    story = issue_description_text(by_key["S1"])
    assert "S1" in story
    assert "F-1" in story
    assert "C-1" in story
    assert "Given a, when b, then c" in story  # acceptance criteria travel too
    assert "The PRD" in story and "The architecture" in story

    epic = issue_description_text(by_key["E1"])
    assert "E1" in epic and "F-1" in epic

    task = issue_description_text(by_key["T1"])
    assert "T1" in task and "S1" in task and "C-1" in task


def test_a_summary_jira_would_refuse_is_never_proposed():
    plan = ImplementationPlan(
        stories=(
            Story(story_id="S1", title="A title\nwith a break", feature_ids=("F-1",)),
            Story(story_id="S2", title="x" * 400, feature_ids=("F-1",)),
        )
    )

    work_plan = map_plan_to_work_plan(plan, _metadata(), PROJECT)

    for issue in work_plan.issues:
        assert "\n" not in issue.summary
        assert len(issue.summary) <= 255
    assert validate_work_plan(work_plan, _metadata(), PROJECT) == ()


# --- The stable plan item -> Jira key mapping ----------------------------


def test_the_mapping_is_keyed_by_the_plans_own_identifiers():
    work_plan = _mapped()

    mapping = record_created_issues(
        DeliveryMapping(project_identifier=PROJECT.api_identifier),
        (_ok("E1", "ENG-1", "First epic"), _ok("S1", "ENG-2"), _ok("T1", "ENG-3")),
        work_plan,
        _plan(),
    )

    assert mapping.issue_keys == {"E1": "ENG-1", "S1": "ENG-2", "T1": "ENG-3"}
    assert mapping.key_for("S1") == "ENG-2"
    assert mapping.key_for("nothing") == ""
    assert mapping.created_count == 3
    # And the traceability is copied onto the link, not looked up in a plan that the
    # session may have replaced by the time anyone reads this.
    link = mapping.link_for("S1")
    assert link.feature_ids == ("F-1",)
    assert link.component_ids == ("C-1",)
    assert link.issue_type_name == "Story"
    assert link.work_type == plan_models.STORY


def test_the_mapping_survives_the_plan_being_mapped_again():
    """A rerun re-derives the proposal; the keys must not move."""
    mapping = record_created_issues(
        DeliveryMapping(), (_ok("S1", "ENG-2"),), _mapped(), _plan()
    )

    remapped = _mapped()

    assert pending_plan_keys(remapped, mapping) == ("E1", "E2", "S2", "T1", "T2")
    assert mapping.key_for("S1") == "ENG-2"


def test_a_failure_is_not_recorded_as_a_link():
    """A link means "this exists in Jira". Recording a failure would skip a real retry."""
    mapping = record_created_issues(
        DeliveryMapping(), (_ok("E1", "ENG-1"), _failed("S1")), _mapped(), _plan()
    )

    assert mapping.issue_keys == {"E1": "ENG-1"}
    assert mapping.link_for("S1") is None
    assert "S1" in pending_plan_keys(_mapped(), mapping)


# --- Idempotence and recovery -------------------------------------------


def test_recording_the_same_result_twice_leaves_one_link():
    work_plan = _mapped()
    first = record_created_issues(DeliveryMapping(), (_ok("E1", "ENG-1"),), work_plan, _plan())

    again = record_created_issues(first, (_ok("E1", "ENG-1"),), work_plan, _plan())

    assert len(again.links) == 1
    assert again.issue_keys == {"E1": "ENG-1"}


def test_an_already_created_item_is_excluded_from_the_next_run():
    work_plan = _mapped()
    mapping = record_created_issues(
        DeliveryMapping(), (_ok("E1", "ENG-1"), _ok("S1", "ENG-2")), work_plan, _plan()
    )

    assert known_issue_keys(mapping) == {"E1": "ENG-1", "S1": "ENG-2"}
    assert pending_plan_keys(work_plan, mapping) == ("E2", "S2", "T1", "T2")


def test_nothing_is_pending_once_everything_is_created():
    work_plan = _mapped()
    results = tuple(
        _ok(issue.plan_key, "ENG-{}".format(n))
        for n, issue in enumerate(work_plan.issues, start=1)
    )

    mapping = record_created_issues(DeliveryMapping(), results, work_plan, _plan())

    assert pending_plan_keys(work_plan, mapping) == ()
    assert delivery_progress(work_plan, mapping).is_complete is True


# --- Delivery status, read from creation evidence ------------------------


def test_progress_counts_what_exists_what_failed_and_what_cannot_be_created():
    metadata = _metadata(("1", "Epic", False, 1), ("2", "Story", False, 0))
    work_plan = map_plan_to_work_plan(_plan(), metadata, PROJECT)
    results = (_ok("E1", "ENG-1"), _failed("S1"))
    mapping = record_created_issues(DeliveryMapping(), results, work_plan, _plan())

    progress = delivery_progress(work_plan, mapping, results)

    assert progress.total == 4  # E1, E2, S1, S2 -- the two tasks do not fit
    assert progress.created == 1
    assert progress.failed == 1
    assert progress.excluded == 2
    assert progress.pending == 3
    assert progress.is_complete is False
    assert progress.has_started is True


def test_an_empty_mapping_reports_nothing_rather_than_something():
    empty = DeliveryMapping()

    assert empty.is_empty is True
    assert empty.created_count == 0
    assert known_issue_keys(empty) == {}
    assert known_issue_keys(None) == {}
    assert delivery_progress(_mapped(), empty).has_started is False


def test_the_delivery_stage_reads_the_mapping_and_names_no_workflow_status():
    from lifecycle_models import DELIVERY_STATUS, IN_PROGRESS, lifecycle_from

    mapping = DeliveryMapping(links=(DeliveryLink(plan_item_id="E1", issue_key="ENG-1"),))

    state = lifecycle_from(delivery_mapping=mapping).state(DELIVERY_STATUS)

    assert state.status == IN_PROGRESS
    assert "implementation-plan item(s) created" in state.detail
    # Creation evidence, never a claim about what Jira's workflow says.
    assert "no Jira workflow status is read" in state.detail
    # And without a mapping the stage does not claim delivery.
    assert lifecycle_from().state(DELIVERY_STATUS).status != IN_PROGRESS


def test_the_jira_service_still_has_no_way_to_read_an_issue_back():
    """
    The requirement-drift feature's per-issue read stays removed. "Status readback" here
    means the creation evidence this app owns, and adding an issue read would rebuild the
    surface that fed Jira -> BRD synchronization.
    """
    import jira_service

    assert not hasattr(jira_service.JiraService, "get_issue_fields")
    assert not hasattr(jira_service.JiraService, "get_issue")
    assert not hasattr(jira_service.JiraService, "search_issues")
    assert hasattr(jira_service.JiraService, "create_issue")

    # And this module cannot reach Jira at all: no HTTP client, no service.
    source = open(delivery.__file__, encoding="utf-8").read()
    for forbidden in ("requests", "jira_service", "get_issue", "http"):
        assert forbidden not in source.lower(), forbidden


def test_delivery_cannot_change_the_plan_the_prd_or_the_architecture():
    """One-way traceability, asserted on the objects rather than argued for."""
    plan = _plan()
    before = (
        plan.epics,
        plan.stories,
        plan.source_prd_title,
        plan.source_architecture_title,
        plan.notes,
    )

    work_plan = _mapped(plan)
    mapping = record_created_issues(
        DeliveryMapping(), (_ok("E1", "ENG-1"),), work_plan, plan
    )
    delivery_progress(work_plan, mapping)

    assert (
        plan.epics,
        plan.stories,
        plan.source_prd_title,
        plan.source_architecture_title,
        plan.notes,
    ) == before


def test_the_mapper_is_domain_independent():
    """
    E-commerce is a demonstration domain. A module that named one would be a module
    that could not serve the next.
    """
    source = open(delivery.__file__, encoding="utf-8").read().lower()

    for word in ("ecommerce", "e-commerce", "checkout", "cart", "shopper", "payment"):
        assert word not in source, word


# --- Session state -------------------------------------------------------
# Everything below needs Streamlit and main. Imported inside each test so the pure
# mapping tests above stay runnable without them.


class _Tokens:
    """The only things the creation loop asks a token set."""

    access_token = "not-a-real-token"

    def __init__(self, scopes=("write:jira-work",)):
        self._scopes = scopes

    def is_expired(self):
        return False

    def can_refresh(self):
        return True

    def public_summary(self):
        return {"scopes": list(self._scopes)}


class _Service:
    """A Jira service that records every create instead of making one."""

    WRITE_SCOPE = "write:jira-work"
    name = "jira"
    display_name = "Jira"

    def __init__(self, fail_on=(), start=0):
        self.calls = []
        self.fail_on = set(fail_on)
        self._next = start

    def create_issue(self, access_token, cloud_id, payload):
        summary = payload["fields"]["summary"]
        self.calls.append(payload)
        if summary in self.fail_on:
            raise RuntimeError("Jira returned HTTP 400")
        self._next += 1
        return {"key": "ENG-{}".format(self._next), "id": str(self._next)}


def test_an_unapproved_plan_is_refused_before_anything_is_mapped():
    import streamlit as st

    import main

    st.session_state.clear()
    # Nothing held at all.
    plan, reason = main._approved_implementation_plan()
    assert plan is None and "Implementation Plan stage" in reason

    # Held, but not approved: the gate is the approval, not the artifact.
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = _plan()
    plan, reason = main._approved_implementation_plan()
    assert plan is None
    assert "not approved" in reason

    st.session_state[main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = True
    plan, reason = main._approved_implementation_plan()
    assert plan is not None and reason == ""


def test_an_empty_plan_is_never_treated_as_approved_work():
    import streamlit as st

    import main

    st.session_state.clear()
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = ImplementationPlan()
    st.session_state[main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = True

    plan, reason = main._approved_implementation_plan()

    assert plan is None and reason


def test_rendering_the_delivery_panel_writes_nothing_without_a_confirmation(monkeypatch):
    """
    The property that makes this safe to render on every rerun: the write lives in the
    branch a confirm button was pressed in, and no button is pressed here.
    """
    import streamlit as st

    import main

    st.session_state.clear()
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = _plan()
    st.session_state[main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = True

    monkeypatch.setattr(main.st, "button", lambda *a, **k: False)
    for name in ("markdown", "caption", "write", "info", "warning", "success"):
        monkeypatch.setattr(main.st, name, lambda *a, **k: None)
    service = _Service()
    site = type("Site", (), {"id": "cloud-1", "url": "https://x.atlassian.net"})()

    main._render_plan_delivery_panel(service, _Tokens(), site, PROJECT, _metadata())

    assert service.calls == []
    assert st.session_state.get(main._skey(main.JIRA_STATE_NAME, "delivery_mapping")) is None


def test_a_session_without_the_write_scope_is_told_before_it_tries(monkeypatch):
    import streamlit as st

    import main

    st.session_state.clear()
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = _plan()
    st.session_state[main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = True

    warned = []
    monkeypatch.setattr(main.st, "warning", lambda text, **k: warned.append(str(text)))
    for name in ("markdown", "caption", "write", "info", "success"):
        monkeypatch.setattr(main.st, name, lambda *a, **k: None)
    monkeypatch.setattr(
        main.st, "button", lambda *a, **k: pytest.fail("no write is offered")
    )
    service = _Service()
    site = type("Site", (), {"id": "cloud-1", "url": "https://x.atlassian.net"})()

    main._render_plan_delivery_panel(
        service, _Tokens(scopes=("read:jira-work",)), site, PROJECT, _metadata()
    )

    assert any("write:jira-work" in text for text in warned)
    assert service.calls == []


def test_a_retry_creates_only_what_is_missing_and_reparents_nothing():
    """
    The idempotence guarantee, exercised through the real creation loop.

    Run one fails at the first story, so the epics exist and nothing below them does.
    Run two must not re-create an epic -- Jira would make a second one -- and must
    parent the story onto the key its epic actually got.
    """
    import streamlit as st

    import main

    st.session_state.clear()
    plan = _plan()
    work_plan = _mapped(plan)

    first = _Service(fail_on=["First story"])
    run_one = main._create_selected_issues(
        first, _Tokens(), "cloud-1", PROJECT, work_plan
    )
    mapping = record_created_issues(DeliveryMapping(), run_one, work_plan, plan)

    # Parents first, and the run stops where it broke rather than orphaning children.
    assert [record.plan_key for record in run_one] == ["E1", "E2", "S1"]
    assert mapping.issue_keys == {"E1": "ENG-1", "E2": "ENG-2"}
    epic_key = mapping.key_for("E1")

    second = _Service(start=10)
    run_two = main._create_selected_issues(
        second, _Tokens(), "cloud-1", PROJECT, work_plan, known_keys=known_issue_keys(mapping)
    )

    # Neither epic was sent a second time.
    summaries = [payload["fields"]["summary"] for payload in second.calls]
    assert "First epic" not in summaries and "Second epic" not in summaries
    assert [record.plan_key for record in run_two] == ["S1", "S2", "T1", "T2"]
    # And the story was parented onto the issue the earlier run created.
    story_payload = next(
        payload for payload in second.calls if payload["fields"]["summary"] == "First story"
    )
    assert story_payload["fields"]["parent"] == {"key": epic_key}

    final = record_created_issues(mapping, run_two, work_plan, plan)
    assert final.key_for("E1") == epic_key  # unchanged by the second run
    assert pending_plan_keys(work_plan, final) == ()
    assert len(final.links) == len([i for i in work_plan.issues if i.selected])


def test_running_twice_with_nothing_left_sends_nothing():
    import streamlit as st

    import main

    st.session_state.clear()
    work_plan = _mapped()
    service = _Service()
    run = main._create_selected_issues(service, _Tokens(), "cloud-1", PROJECT, work_plan)
    mapping = record_created_issues(DeliveryMapping(), run, work_plan, _plan())

    quiet = _Service()
    again = main._create_selected_issues(
        quiet, _Tokens(), "cloud-1", PROJECT, work_plan, known_keys=known_issue_keys(mapping)
    )

    assert quiet.calls == []
    assert again == ()


def test_the_brd_creation_path_is_unchanged_by_the_new_argument():
    """
    Phase 5 added a keyword to the shared creation loop. The BRD path passes nothing, so
    it must behave exactly as before: every selected issue is sent once.
    """
    import streamlit as st

    import main
    import test_jira_creation as brd_path

    st.session_state.clear()
    plan = brd_path.a_plan()
    service = _Service()

    results = main._create_selected_issues(
        service, _Tokens(), "cloud-1", brd_path.PROJECT, plan
    )

    assert len(service.calls) == len(creation_order(plan))
    assert all(record.succeeded for record in results)


def test_choosing_a_different_project_drops_the_delivery_mapping():
    """
    A mapping's keys only mean anything in the project they were created in, so it is
    cleared with the rest of the project-bound state rather than carried across.
    """
    import main

    for suffix in (
        "delivery_mapping",
        "delivery_results",
        "delivery_creating",
        "delivery_confirm",
    ):
        assert suffix in main._JIRA_PROJECT_SUFFIXES, suffix
    # The removed governance state stays removed.
    for gone in ("changes", "change_baseline"):
        assert gone not in main._JIRA_PROJECT_SUFFIXES


def test_the_delivery_stage_renders_the_mapping_without_a_token(monkeypatch):
    import streamlit as st

    import main

    st.session_state.clear()
    st.session_state[main._skey(main.JIRA_STATE_NAME, "delivery_mapping")] = DeliveryMapping(
        project_label="Engineering",
        site_url="https://x.atlassian.net",
        links=(
            DeliveryLink(
                plan_item_id="S1",
                issue_key="ENG-2",
                issue_type_name="Story",
                summary="First story",
                feature_ids=("F-1",),
                component_ids=("C-1",),
            ),
        ),
    )
    shown = []
    for name in ("markdown", "caption", "write", "info", "success"):
        monkeypatch.setattr(main.st, name, lambda text, **k: shown.append(str(text)))
    monkeypatch.setattr(
        main.JiraService, "__init__", lambda *a, **k: pytest.fail("no service is built")
    )

    main._render_plan_delivery_status(None)

    rendered = " ".join(shown)
    assert "S1" in rendered and "ENG-2" in rendered
    assert "F-1" in rendered and "C-1" in rendered
    assert "https://x.atlassian.net" in rendered
