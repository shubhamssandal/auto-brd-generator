"""
JIRA-005 tests: BRD-to-Jira work-plan generation.

The plan is built from a ``BRDData`` and a ``JiraProjectMetadata`` constructed
directly, rather than through the service: JIRA-004 already covers how metadata is
retrieved, and building it here keeps each case about the mapping.

Two deliberate choices run through the whole file.

The issue types are named **Initiative / Deliverable / Step**, never Epic / Story /
Sub-task. ``PRD.md`` forbids assuming every project uses the same hierarchy, so if
any part of the mapping quietly matched on Jira's default names instead of the
reported ``hierarchyLevel`` and ``subtask`` flag, nothing here would work.

The autouse fixtures come from ``test_jira_sites`` -- a clean session per test and a
hard failure on any write verb. Plan generation should reach Jira not at all, which
several tests assert directly.
"""

import inspect

import pytest
import streamlit as st

import jira_processor
import jira_service
import main
from brd_models import ActionItem, BRDData, ProjectOverview, Requirement
from jira_models import (
    MAX_SUMMARY_LENGTH,
    JiraField,
    JiraIssueType,
    JiraProject,
    JiraProjectMetadata,
    JiraWorkPlan,
    PlannedIssue,
)
from jira_processor import build_work_plan, choose_issue_types
from test_jira_sites import (  # noqa: F401 -- fixtures are used by pytest, not by name
    clean_session,
    forbid_write_requests,
)

SITE_ID = "1324a887-45db-1bf4-1e99-ef0ff456d421"
PROJECT = JiraProject(id="10001", key="ENG", name="Engineering", project_type_key="software")
SCOPE = (SITE_ID, PROJECT.api_identifier)

SUPPORTED_FIELDS = (JiraField(field_id="summary", name="Summary", required=True),)
BLOCKING_FIELDS = SUPPORTED_FIELDS + (
    JiraField(field_id="customfield_10101", name="Squad", required=True),
)


def a_type(type_id, name, level=None, subtask=False, fields=SUPPORTED_FIELDS, known=True):
    """A discovered issue type. Defaults to one that passed the required-field check."""
    return JiraIssueType(
        id=type_id,
        name=name,
        subtask=subtask,
        hierarchy_level=level,
        required_fields=fields,
        fields_known=known,
    )


# Names Jira's defaults never use, at levels that carry no conventional meaning. Any
# mapping that keys off a name rather than off the reported level fails on these.
CONTAINER = a_type("10", "Initiative", level=2)
ITEM = a_type("20", "Deliverable", level=0)
SUBTASK = a_type("30", "Step", level=-1, subtask=True)


def a_metadata(*types, project=PROJECT, notes=(), truncated=False):
    return JiraProjectMetadata(
        project_identifier=project.api_identifier,
        issue_types=tuple(types),
        notes=tuple(notes),
        truncated=truncated,
    )


def a_brd(**overrides):
    """A small BRD whose ids, evidence and action item are what the mapping keys off."""
    data = dict(
        project_title="Payments Revamp",
        project_overview=ProjectOverview(project_name="Payments Revamp", date="2026-08-20"),
        business_objectives=["Reduce checkout abandonment."],
        functional_requirements=[
            Requirement("FR-1", "The system shall accept card payments.", "we need card payments"),
            Requirement("FR-2", "The system shall email a receipt.", "always email the receipt"),
        ],
        non_functional_requirements=[
            Requirement("NFR-1", "The system must respond within 2 seconds.", "under two seconds"),
        ],
        acceptance_criteria=[
            "FR-1 is met when a card payment is accepted end to end.",
            "Every screen meets the accessibility standard.",
        ],
        action_items=[ActionItem(item="Spike the gateway SDK for FR-1", owner="Dana")],
    )
    data.update(overrides)
    return BRDData(**data)


def issues_by_key(plan):
    return {issue.plan_key: issue for issue in plan.issues}


def render_plan_panel(metadata, monkeypatch, project=PROJECT, scope=SCOPE, clicked=False):
    """
    Render the work-plan panel and capture what the user would see.

    Mirrors ``render_projects_panel`` in ``test_jira_projects``: widgets are replaced
    with recorders, the expander header is recorded and then handed to the real one,
    which works unpatched in pytest's bare mode.
    """
    shown = {
        key: []
        for key in ("success", "warning", "error", "info", "caption", "markdown", "write")
    }
    for widget in shown:
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, _w=widget, **k: shown[_w].append(str(text))
        )
    monkeypatch.setattr(main.st, "button", lambda label, **k: clicked)

    shown["expander"] = []
    real_expander = main.st.expander

    def recording_expander(label, *args, **kwargs):
        shown["expander"].append(str(label))
        return real_expander(label, *args, **kwargs)

    monkeypatch.setattr(main.st, "expander", recording_expander)

    main._render_jira_work_plan_panel(project, metadata, scope)
    return shown


# --- Hierarchy comes from the project, never from a name -------------------

def test_three_reported_levels_produce_container_items_and_subtasks():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(CONTAINER, ITEM, SUBTASK))

    root = plan.roots[0]
    assert len(plan.roots) == 1, "one container issue groups the plan"
    assert root.issue_type_name == "Initiative"
    assert [child.plan_key for child in plan.children_of(root.plan_key)] == [
        "FR-1",
        "FR-2",
        "NFR-1",
    ]
    assert {child.issue_type_name for child in plan.children_of(root.plan_key)} == {"Deliverable"}
    # The action item names FR-1, and this project has a subtask level to put it at.
    subtasks = plan.children_of("FR-1")
    assert [s.issue_type_name for s in subtasks] == ["Step"]
    assert "gateway SDK" in subtasks[0].summary


def test_issue_types_are_ordered_by_reported_level_not_by_name():
    """
    The container is chosen because Jira put it at a higher level, not because it is
    called anything in particular -- here the higher level has the lower-sounding name.
    """
    metadata = a_metadata(
        a_type("1", "Small thing", level=0),
        a_type("2", "Zzz umbrella", level=7),
    )

    container, item, subtask = choose_issue_types(metadata)

    assert container.name == "Zzz umbrella"
    assert item.name == "Small thing"
    assert subtask is None


def test_a_single_reported_level_produces_a_flat_plan():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(ITEM))

    assert [issue.plan_key for issue in plan.roots] == ["FR-1", "FR-2", "NFR-1"]
    assert all(not issue.parent_plan_key for issue in plan.issues)
    assert any("plan is flat" in note for note in plan.notes)


def test_no_subtask_type_means_no_subtask_is_proposed():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(CONTAINER, ITEM))

    assert all(issue.issue_type_name != "Step" for issue in plan.issues)
    assert plan.children_of("FR-1") == ()
    assert any("no validated subtask type" in note for note in plan.notes)


def test_a_type_reporting_no_level_is_used_only_as_a_flat_item_level():
    """
    Nothing can be ordered against an unreported level, so such a type carries a flat
    plan when it is all there is, and loses to a levelled type when one exists.
    """
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(a_type("40", "Unlevelled")))
    assert {issue.issue_type_name for issue in plan.issues} == {"Unlevelled"}
    assert plan.roots == plan.issues

    container, item, _ = choose_issue_types(
        a_metadata(a_type("40", "Unlevelled"), ITEM, CONTAINER)
    )
    assert item.name == "Deliverable"
    assert container.name == "Initiative"


def test_the_first_type_jira_listed_at_a_level_is_used():
    """
    Several types commonly share one level. Choosing between them on any basis other
    than Jira's own ordering would mean ranking names, which JIRA-006 lets the user do.
    """
    first = a_type("21", "Chore", level=0)
    metadata = a_metadata(first, a_type("22", "Defect", level=0), a_type("23", "Aim", level=0))

    _, item, _ = choose_issue_types(metadata)

    assert item is first


def test_no_hierarchy_name_is_invented_anywhere_in_the_plan(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_brd()

    shown = render_plan_panel(a_metadata(CONTAINER, ITEM, SUBTASK), monkeypatch, clicked=True)

    rendered = " ".join(text for texts in shown.values() for text in texts)
    assert "Initiative" in rendered and "Deliverable" in rendered and "Step" in rendered
    for invented in ("Epic", "Story", "Sub-task", "Subtask"):
        assert invented not in rendered, invented


# --- Only validated issue types are used ----------------------------------

@pytest.mark.parametrize(
    "rejected",
    [
        # Jira would refuse it for want of a field no BRD can supply.
        a_type("50", "Blocked thing", level=0, fields=BLOCKING_FIELDS),
        # Its required fields could not be read, which is not the same as "none".
        a_type("60", "Unchecked thing", level=0, fields=(), known=False),
    ],
    ids=["blocked", "unchecked"],
)
def test_an_issue_type_that_did_not_pass_validation_is_not_used(rejected):
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(rejected, ITEM))

    assert {issue.issue_type_id for issue in plan.issues} == {ITEM.id}


@pytest.mark.parametrize(
    "types, expected_note",
    [
        ((), "Jira reported no issue type"),
        (
            (a_type("50", "Blocked thing", level=0, fields=BLOCKING_FIELDS),),
            "none of the 1 issue type(s)",
        ),
        # A subtask type passes validation but cannot stand alone, so saying it failed
        # the field check would be untrue -- the note has to give the real reason.
        ((SUBTASK,), "is a subtask type, and a subtask needs a parent"),
    ],
    ids=["no-types", "all-blocked", "subtask-only"],
)
def test_a_project_with_no_usable_item_level_produces_no_plan_and_says_why(
    types, expected_note
):
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(*types))

    assert plan.is_empty
    assert any(expected_note in note for note in plan.notes), plan.notes


def test_incomplete_metadata_is_reported_before_the_plan_is_trusted():
    plan = build_work_plan(
        a_brd(), PROJECT, a_metadata(CONTAINER, ITEM, truncated=True)
    )

    assert any("incomplete list" in note for note in plan.notes)
    assert not plan.is_empty, "an incomplete list is still worth planning from"


def test_metadata_read_for_another_project_is_refused():
    """
    Planning against another project's issue types is exactly the assumed-hierarchy
    mistake the PRD forbids, so it is refused rather than attempted.
    """
    other = a_metadata(CONTAINER, ITEM, project=JiraProject(id="99999", key="OTHER", name="Other"))

    plan = build_work_plan(a_brd(), PROJECT, other)

    assert plan.is_empty
    assert any("not for" in note for note in plan.notes)


def test_the_plan_is_built_from_the_declared_models():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(CONTAINER, ITEM, SUBTASK))

    assert isinstance(plan, JiraWorkPlan)
    assert plan.issues and all(isinstance(issue, PlannedIssue) for issue in plan.issues)
    assert plan.project_identifier == PROJECT.api_identifier
    assert plan.project_label == PROJECT.display_label


# --- Descriptions and acceptance criteria ---------------------------------

def test_a_requirement_issue_carries_its_statement_id_and_verbatim_evidence():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(CONTAINER, ITEM))
    issue = issues_by_key(plan)["FR-1"]

    assert issue.summary == "The system shall accept card payments."
    assert "The system shall accept card payments." in issue.description
    assert "FR-1" in issue.description
    # The transcript quote the requirement had to prove travels with the issue.
    assert "we need card payments" in issue.description
    assert issue.source_requirement_id == "FR-1"


def test_the_container_issue_carries_the_brd_title_and_objectives():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(CONTAINER, ITEM))
    root = plan.roots[0]

    assert root.summary == "Payments Revamp"
    assert "Reduce checkout abandonment." in root.description
    assert "2026-08-20" in root.description
    assert not root.source_requirement_id, "the container restates no single requirement"


def test_an_acceptance_criterion_attaches_to_the_requirement_it_names():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(CONTAINER, ITEM))
    by_key = issues_by_key(plan)

    assert by_key["FR-1"].acceptance_criteria == (
        "FR-1 is met when a card payment is accepted end to end.",
    )
    assert by_key["FR-2"].acceptance_criteria == ()
    # One that names nothing stays at BRD level, on the grouping issue.
    assert plan.roots[0].acceptance_criteria == ("Every screen meets the accessibility standard.",)


def test_a_requirement_id_does_not_match_a_longer_id_that_starts_with_it():
    """
    Attaching FR-1's criterion to FR-12 would be worse than leaving it unattached.
    """
    brd = a_brd(
        functional_requirements=[
            Requirement("FR-1", "Short one.", "short"),
            Requirement("FR-12", "Longer one.", "longer"),
        ],
        non_functional_requirements=[],
        acceptance_criteria=["FR-12 is met when the longer one works."],
        action_items=[],
    )

    by_key = issues_by_key(build_work_plan(brd, PROJECT, a_metadata(CONTAINER, ITEM)))

    assert by_key["FR-1"].acceptance_criteria == ()
    assert by_key["FR-12"].acceptance_criteria == ("FR-12 is met when the longer one works.",)


def test_unattached_criteria_are_reported_when_there_is_no_grouping_issue():
    plan = build_work_plan(a_brd(), PROJECT, a_metadata(ITEM))

    assert any("acceptance criterion" in note for note in plan.notes)
    assert all(
        "Every screen meets the accessibility standard." not in issue.acceptance_criteria
        for issue in plan.issues
    )


def test_a_criterion_naming_two_requirements_attaches_to_both():
    brd = a_brd(acceptance_criteria=["FR-1 and FR-2 both hold at release."], action_items=[])

    by_key = issues_by_key(build_work_plan(brd, PROJECT, a_metadata(CONTAINER, ITEM)))

    assert by_key["FR-1"].acceptance_criteria == by_key["FR-2"].acceptance_criteria
    assert len(by_key["FR-1"].acceptance_criteria) == 1


# --- Action items become subtasks only where that is supported ------------

def test_an_action_item_naming_no_requirement_is_reported_not_guessed():
    brd = a_brd(action_items=[ActionItem(item="Book the follow-up meeting", owner="Sam")])

    plan = build_work_plan(brd, PROJECT, a_metadata(CONTAINER, ITEM, SUBTASK))

    assert all("follow-up meeting" not in issue.summary for issue in plan.issues)
    assert any("name no requirement" in note for note in plan.notes)


def test_a_subtask_records_its_owner_and_due_date_when_the_meeting_stated_them():
    brd = a_brd(
        action_items=[ActionItem(item="Spike the SDK for FR-2", owner="Dana", due_date="2026-09-01")]
    )

    plan = build_work_plan(brd, PROJECT, a_metadata(CONTAINER, ITEM, SUBTASK))
    subtask = plan.children_of("FR-2")[0]

    assert "Dana" in subtask.description
    assert "2026-09-01" in subtask.description
    assert subtask.parent_plan_key == "FR-2"


def test_duplicate_requirement_ids_do_not_collide_or_cross_parent():
    """
    Requirement ids come from a language model. A repeated id must not make one
    subtask appear under two issues.
    """
    brd = a_brd(
        functional_requirements=[
            Requirement("FR-1", "First reading.", "first"),
            Requirement("FR-1", "Second reading.", "second"),
        ],
        non_functional_requirements=[],
        action_items=[ActionItem(item="Do the thing for FR-1")],
    )

    plan = build_work_plan(brd, PROJECT, a_metadata(CONTAINER, ITEM, SUBTASK))

    keys = [issue.plan_key for issue in plan.issues]
    assert len(keys) == len(set(keys)), keys
    parents = [issue.parent_plan_key for issue in plan.issues if issue.issue_type_name == "Step"]
    assert len(parents) == 1


# --- Jira's own field constraints ----------------------------------------

def test_a_long_multiline_statement_is_reduced_to_a_summary_jira_accepts():
    statement = "Line one of the requirement.\nLine two.\n" + ("verbose " * 80)
    brd = a_brd(
        functional_requirements=[Requirement("FR-1", statement, "evidence")],
        non_functional_requirements=[],
        acceptance_criteria=[],
        action_items=[],
    )

    issue = issues_by_key(build_work_plan(brd, PROJECT, a_metadata(ITEM)))["FR-1"]

    assert "\n" not in issue.summary
    assert len(issue.summary) <= MAX_SUMMARY_LENGTH
    assert issue.summary.endswith("…"), "a cut summary should show that it was cut"
    # Nothing is lost: the full statement is still in the description.
    assert "Line two." in issue.description


def test_a_requirement_with_no_statement_is_not_proposed():
    brd = a_brd(
        functional_requirements=[
            Requirement("FR-1", "   ", "evidence"),
            Requirement("FR-2", "A real one.", "evidence"),
        ],
        non_functional_requirements=[],
        action_items=[],
    )

    plan = build_work_plan(brd, PROJECT, a_metadata(ITEM))

    assert [issue.plan_key for issue in plan.issues] == ["FR-2"]


def test_a_brd_with_no_confirmed_requirement_produces_no_plan_and_says_why():
    brd = a_brd(
        functional_requirements=[],
        non_functional_requirements=[],
        acceptance_criteria=[],
        action_items=[],
    )

    plan = build_work_plan(brd, PROJECT, a_metadata(CONTAINER, ITEM))

    assert plan.is_empty
    assert any("no confirmed requirement" in note for note in plan.notes)


# --- Generating a plan creates nothing -----------------------------------

def test_nothing_in_this_feature_has_a_way_to_reach_jira():
    source = inspect.getsource(jira_processor)

    assert "import requests" not in source
    assert not hasattr(jira_processor, "requests")
    for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
        assert verb not in source
    # No token can be handed to either, so neither can be authorized to do anything.
    assert "access_token" not in inspect.signature(build_work_plan).parameters
    assert "tokens" not in inspect.signature(build_work_plan).parameters
    panel = inspect.signature(main._render_jira_work_plan_panel).parameters
    assert list(panel) == ["project", "metadata", "scope"]


def test_generating_a_plan_through_the_ui_sends_no_request(monkeypatch):
    """The strongest form of "creates nothing": every HTTP verb is a test failure."""
    for verb in ("get", "post", "put", "patch", "delete"):
        monkeypatch.setattr(
            jira_service.requests,
            verb,
            lambda *a, **k: pytest.fail("generating a work plan must send no request"),
            raising=False,
        )
    st.session_state[main.BRD_SESSION_KEY] = a_brd()

    shown = render_plan_panel(a_metadata(CONTAINER, ITEM, SUBTASK), monkeypatch, clicked=True)

    plan = st.session_state[main._skey("jira", "plan")]
    assert not plan.is_empty
    assert any("Nothing has been created in Jira" in text for text in shown["success"])


# --- Panel state ---------------------------------------------------------

def test_nothing_is_generated_until_the_button_is_pressed(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_brd()

    shown = render_plan_panel(a_metadata(CONTAINER, ITEM), monkeypatch, clicked=False)

    assert main._skey("jira", "plan") not in st.session_state
    assert any("Not generated yet" in text for text in shown["caption"])


def test_no_brd_means_no_plan_is_offered(monkeypatch):
    shown = render_plan_panel(a_metadata(CONTAINER, ITEM), monkeypatch, clicked=True)

    assert main._skey("jira", "plan") not in st.session_state
    assert any("No BRD is available yet" in text for text in shown["caption"])


def test_a_plan_for_another_target_is_dropped_rather_than_shown(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_brd()
    st.session_state[main._skey("jira", "plan")] = "stale plan"
    st.session_state[main._skey("jira", "plan_for")] = ("other-site", "10001")

    render_plan_panel(a_metadata(CONTAINER, ITEM), monkeypatch, clicked=False)

    assert main._skey("jira", "plan") not in st.session_state
    assert main._skey("jira", "plan_for") not in st.session_state


def test_a_newly_generated_brd_drops_a_plan_built_from_the_previous_one():
    st.session_state[main._skey("jira", "plan")] = "plan from the old BRD"
    st.session_state[main._skey("jira", "plan_for")] = SCOPE
    fresh = a_brd(project_title="A different meeting")

    main._store_brd(fresh)

    assert st.session_state[main.BRD_SESSION_KEY] is fresh
    assert main._skey("jira", "plan") not in st.session_state
    assert main._skey("jira", "plan_for") not in st.session_state


@pytest.mark.parametrize(
    "clear",
    [main._disconnect, main._clear_jira_project_state],
    ids=["disconnect", "project-change"],
)
def test_the_plan_is_cleared_by_every_path_that_invalidates_it(clear):
    """
    Disconnecting and changing the project both have to drop the plan. Token clearing
    is `_disconnect`'s own business and is covered by the earlier tickets' tests.
    """
    for suffix in main._JIRA_PROJECT_SUFFIXES:
        st.session_state[main._skey("jira", suffix)] = "anything"

    clear(jira_service.JiraService())

    assert [key for key in st.session_state if key.startswith("jira__")] == []


def test_an_empty_plan_is_shown_as_empty_with_its_reason(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_brd()

    shown = render_plan_panel(a_metadata(), monkeypatch, clicked=True)

    assert st.session_state[main._skey("jira", "plan")].is_empty
    assert any("No issue could be proposed" in text for text in shown["warning"])
    assert not shown["success"]


def test_the_rendered_plan_shows_the_parent_child_relationship(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_brd()

    shown = render_plan_panel(a_metadata(CONTAINER, ITEM, SUBTASK), monkeypatch, clicked=True)

    lines = shown["markdown"]
    parent_line = next(line for line in lines if "Payments Revamp" in line)
    child_line = next(line for line in lines if "accept card payments" in line)
    subtask_line = next(line for line in lines if "gateway SDK" in line)

    assert "↳" not in parent_line
    assert child_line.count("&nbsp;") < subtask_line.count("&nbsp;")
    assert "↳" in child_line and "↳" in subtask_line
    # Every proposed issue is reachable in the detail panes, by plan key.
    assert any("FR-1" in header for header in shown["expander"])
