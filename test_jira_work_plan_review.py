"""
JIRA-006 tests: reviewing a generated Jira work plan before anything is created.

The five acceptance criteria are what this file is organised around -- editing
fields, deleting items, selecting and deselecting, preserving the hierarchy, and
validating parent/child relationships after an edit.

Everything reuses JIRA-005's builders (``a_brd``, ``a_metadata``, ``a_type`` and the
``Initiative / Deliverable / Step`` types) and JIRA-003's autouse fixtures -- a clean
session per test, and a hard failure on any HTTP verb that could change something in
Jira. Reviewing a plan must reach Jira not at all, which the last section asserts
directly.

The issue types keep JIRA-005's deliberately unconventional names so that a review
step which quietly matched on "Epic" or "Sub-task" instead of the reported
``hierarchyLevel`` and ``subtask`` flag would fail here.
"""

import inspect
from dataclasses import FrozenInstanceError

import pytest
import streamlit as st

import jira_processor
import jira_service
import main
from jira_models import (
    MAX_SUMMARY_LENGTH,
    JiraField,
    JiraProject,
    JiraWorkPlan,
    PlannedIssue,
)
from jira_processor import (
    build_work_plan,
    compatible_issue_types,
    delete_planned_issue,
    set_planned_issue_type,
    update_planned_issue,
    validate_work_plan,
)
from test_jira_sites import (  # noqa: F401 -- fixtures are used by pytest, not by name
    clean_session,
    forbid_write_requests,
)
from test_jira_work_plan import (
    BLOCKING_FIELDS,
    CONTAINER,
    ITEM,
    PROJECT,
    SCOPE,
    SUBTASK,
    a_brd,
    a_metadata,
    a_type,
    issues_by_key,
)

# A second type sharing ITEM's reported level and subtask flag. A project commonly
# lists several types at one level; switching among exactly those is the review-time
# choice ``FRONTEND_SPEC.md`` calls "issue-type selection where supported".
SIBLING = a_type("21", "Chore", level=0)
# Same level as ITEM, but Jira would refuse it for want of a field no BRD can supply.
BLOCKED_SIBLING = a_type("22", "Blocked thing", level=0, fields=BLOCKING_FIELDS)
# A description Jira marks required and does not fill in by itself.
DESCRIPTION_REQUIRED = JiraField(field_id="description", name="Description", required=True)
DESCRIPTION_DEFAULTED = JiraField(
    field_id="description", name="Description", required=True, has_default_value=True
)

FULL_METADATA = a_metadata(CONTAINER, ITEM, SUBTASK)


def a_plan(*types):
    """A generated plan to review. Defaults to the three-level shape."""
    return build_work_plan(a_brd(), PROJECT, a_metadata(*(types or (CONTAINER, ITEM, SUBTASK))))


def wkey(plan_key, field):
    """The Streamlit widget key ``main`` uses for one editor on one issue."""
    return "{}{}__{}".format(main._JIRA_REVIEW_WIDGET_PREFIX, plan_key, field)


def render_review_panel(
    monkeypatch,
    plan,
    metadata=FULL_METADATA,
    project=PROJECT,
    scope=SCOPE,
    values=None,
    clicked=(),
):
    """
    Render the panel over an already-generated plan and capture what is shown.

    The plan is seeded into session state rather than generated here, because review
    is what happens on the *next* run after generation: every widget interaction
    re-runs the script, so the plan a reviewer edits always arrives from the session.

    ``values`` maps a widget key to what the reviewer left in it, so a test can edit
    one field on one issue and leave everything else at its stored value -- which is
    what an untouched widget returns. ``clicked`` names pressed buttons by key.

    Returns ``(shown, offered)``: the messages the user would see, and the issue-type
    options each selectbox presented.
    """
    st.session_state[main.BRD_SESSION_KEY] = a_brd()
    st.session_state[main._skey("jira", "plan")] = plan
    st.session_state[main._skey("jira", "plan_for")] = scope

    values = dict(values or {})
    shown = {
        key: []
        for key in ("success", "warning", "error", "info", "caption", "markdown", "write")
    }
    for widget in shown:
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, _w=widget, **k: shown[_w].append(str(text))
        )

    def fake_checkbox(label, value=False, key=None, **kwargs):
        return values.get(key, value)

    def fake_text(label, value="", key=None, **kwargs):
        return values.get(key, value)

    offered = []

    def fake_selectbox(label, options=(), index=0, format_func=None, key=None, **kwargs):
        options = list(options)
        offered.append(
            {
                "key": key,
                "options": options,
                "labels": [format_func(o) for o in options] if format_func else [],
                "index": index,
            }
        )
        return values.get(key, options[index] if options else None)

    monkeypatch.setattr(main.st, "checkbox", fake_checkbox)
    monkeypatch.setattr(main.st, "text_input", fake_text)
    monkeypatch.setattr(main.st, "text_area", fake_text)
    monkeypatch.setattr(main.st, "selectbox", fake_selectbox)
    monkeypatch.setattr(main.st, "button", lambda label, **k: k.get("key") in clicked)
    monkeypatch.setattr(main.st, "rerun", lambda *a, **k: None)

    shown["expander"] = []
    real_expander = main.st.expander

    def recording_expander(label, *args, **kwargs):
        shown["expander"].append(str(label))
        return real_expander(label, *args, **kwargs)

    monkeypatch.setattr(main.st, "expander", recording_expander)

    main._render_jira_work_plan_panel(project, metadata, scope)
    return shown, offered


def stored_plan():
    """The plan as the session holds it after a render."""
    return st.session_state[main._skey("jira", "plan")]


# --- Edit supported planned-issue fields -----------------------------------

def test_editing_a_field_returns_a_new_plan_and_leaves_the_original_alone():
    plan = a_plan()

    edited = update_planned_issue(plan, "FR-1", summary="A reworded requirement.")

    assert issues_by_key(edited)["FR-1"].summary == "A reworded requirement."
    assert issues_by_key(plan)["FR-1"].summary == "The system shall accept card payments."
    assert isinstance(edited, JiraWorkPlan)
    assert all(isinstance(issue, PlannedIssue) for issue in edited.issues)


def test_every_supported_field_can_be_edited_at_once():
    plan = update_planned_issue(
        a_plan(),
        "FR-1",
        summary="Reworded.",
        description="Rewritten description.",
        acceptance_criteria=("First.", "Second."),
        selected=False,
    )
    issue = issues_by_key(plan)["FR-1"]

    assert issue.summary == "Reworded."
    assert issue.description == "Rewritten description."
    assert issue.acceptance_criteria == ("First.", "Second.")
    assert issue.selected is False


def test_the_models_stay_frozen_so_an_edit_cannot_happen_in_place():
    issue = a_plan().issues[0]

    with pytest.raises(FrozenInstanceError):
        issue.summary = "mutated"


@pytest.mark.parametrize(
    "field, value",
    [
        ("parent_plan_key", "FR-2"),
        ("issue_type_id", SUBTASK.id),
        ("hierarchy_level", 99),
        ("source_requirement_id", "invented"),
    ],
)
def test_structural_fields_are_not_editable_through_the_field_editor(field, value):
    """
    Plan key, parent and type identity are hierarchy, not content. Letting the field
    editor change them would move an issue in the tree, which is the one thing the
    review step preserves.

    ``plan_key`` is absent from the list because it cannot even be expressed as an
    edit: it is the positional argument naming *which* issue to change, so there is
    no way to pass it as a new value.
    """
    plan = a_plan()

    edited = update_planned_issue(plan, "FR-1", **{field: value})

    assert edited == plan


def test_editing_an_issue_that_is_not_in_the_plan_changes_nothing():
    plan = a_plan()

    assert update_planned_issue(plan, "NOT-A-KEY", summary="x") == plan


def test_an_edited_summary_reaches_the_stored_plan(monkeypatch):
    render_review_panel(
        monkeypatch,
        a_plan(),
        values={wkey("FR-1", "summary"): "Accept card payments at checkout."},
    )

    assert issues_by_key(stored_plan())["FR-1"].summary == "Accept card payments at checkout."


def test_an_edited_description_reaches_the_stored_plan(monkeypatch):
    render_review_panel(
        monkeypatch,
        a_plan(),
        values={wkey("FR-2", "description"): "The receipt email is sent within a minute."},
    )

    assert "within a minute" in issues_by_key(stored_plan())["FR-2"].description


def test_acceptance_criteria_are_edited_as_one_line_each(monkeypatch):
    render_review_panel(
        monkeypatch,
        a_plan(),
        values={wkey("FR-1", "criteria"): "A card payment succeeds.\n\n  A receipt is sent.  "},
    )

    assert issues_by_key(stored_plan())["FR-1"].acceptance_criteria == (
        "A card payment succeeds.",
        "A receipt is sent.",
    )


def test_an_untouched_editor_leaves_the_plan_exactly_as_it_was(monkeypatch):
    plan = a_plan()

    render_review_panel(monkeypatch, plan)

    assert stored_plan() == plan


# --- Delete planned issues -------------------------------------------------

def test_deleting_an_issue_removes_it():
    plan = delete_planned_issue(a_plan(), "FR-2")

    assert "FR-2" not in issues_by_key(plan)
    assert "FR-1" in issues_by_key(plan)


def test_deleting_a_parent_takes_its_descendants_with_it():
    """
    A leftover child would name a parent that no longer exists, which is an
    uncreatable plan. Cascading is what keeps the hierarchy whole.
    """
    plan = a_plan()
    assert plan.children_of("FR-1"), "FR-1 has a subtask to cascade to"

    pruned = delete_planned_issue(plan, "FR-1")

    assert "FR-1" not in issues_by_key(pruned)
    assert pruned.children_of("FR-1") == ()
    assert all(issue.parent_plan_key != "FR-1" for issue in pruned.issues)


def test_deleting_the_container_empties_the_whole_plan():
    plan = a_plan()

    pruned = delete_planned_issue(plan, plan.roots[0].plan_key)

    assert pruned.is_empty, [issue.plan_key for issue in pruned.issues]


def test_no_deletion_ever_leaves_an_issue_naming_a_missing_parent():
    plan = a_plan()

    for issue in plan.issues:
        pruned = delete_planned_issue(plan, issue.plan_key)
        remaining = {kept.plan_key for kept in pruned.issues}
        orphans = [
            kept.plan_key
            for kept in pruned.issues
            if kept.parent_plan_key and kept.parent_plan_key not in remaining
        ]
        assert not orphans, (issue.plan_key, orphans)


def test_deleting_an_issue_that_is_not_in_the_plan_changes_nothing():
    plan = a_plan()

    assert delete_planned_issue(plan, "NOT-A-KEY") == plan


def test_deleting_through_the_ui_prunes_the_stored_plan(monkeypatch):
    render_review_panel(monkeypatch, a_plan(), clicked=(wkey("FR-1", "delete"),))

    remaining = issues_by_key(stored_plan())
    assert "FR-1" not in remaining
    assert "FR-2" in remaining and "NFR-1" in remaining
    # The subtask that named FR-1 went with it rather than being left behind.
    assert all(issue.parent_plan_key != "FR-1" for issue in stored_plan().issues)


def test_deleting_an_issue_clears_the_editor_state_of_the_deleted_subtree(monkeypatch):
    """
    Streamlit keys outlive the widget. A stale value under a deleted issue's key would
    be re-applied to whatever reused that key on the next run.
    """
    st.session_state[wkey("FR-1", "summary")] = "a value the reviewer typed"

    render_review_panel(monkeypatch, a_plan(), clicked=(wkey("FR-1", "delete"),))

    leftovers = [
        key
        for key in st.session_state
        if str(key).startswith(main._JIRA_REVIEW_WIDGET_PREFIX)
    ]
    assert leftovers == []


# --- Select and deselect issues for creation -------------------------------

def test_every_proposed_issue_starts_selected():
    assert all(issue.selected for issue in a_plan().issues)
    assert PlannedIssue(plan_key="X", summary="Y").selected is True


def test_deselecting_an_issue_is_recorded_without_removing_it(monkeypatch):
    render_review_panel(
        monkeypatch, a_plan(), values={wkey("NFR-1", "selected"): False}
    )

    by_key = issues_by_key(stored_plan())
    assert by_key["NFR-1"].selected is False
    assert by_key["NFR-1"].summary, "deselecting keeps the issue and its content"
    assert by_key["FR-1"].selected is True


def test_selected_issues_are_the_ones_a_later_step_would_act_on():
    plan = update_planned_issue(a_plan(), "FR-2", selected=False)

    selected = [issue.plan_key for issue in plan.issues if issue.selected]

    assert "FR-2" not in selected
    assert "FR-1" in selected


# --- Preserve and visibly maintain the hierarchy ---------------------------

def test_the_review_ui_still_shows_the_parent_child_relationship(monkeypatch):
    shown, _ = render_review_panel(monkeypatch, a_plan())

    lines = shown["markdown"]
    parent_line = next(line for line in lines if "Payments Revamp" in line)
    child_line = next(line for line in lines if "accept card payments" in line)
    subtask_line = next(line for line in lines if "gateway SDK" in line)

    assert "↳" not in parent_line
    assert "↳" in child_line and "↳" in subtask_line
    assert child_line.count("&nbsp;") < subtask_line.count("&nbsp;")


def test_editing_a_summary_does_not_flatten_the_hierarchy(monkeypatch):
    """
    The heading is drawn before the expander that holds the editor, so the edit shows
    up on the next run -- which is what a Streamlit interaction always causes. Both
    runs are checked: the tree must survive the edit and still be drawn as a tree.
    """
    shown, _ = render_review_panel(
        monkeypatch, a_plan(), values={wkey("FR-1", "summary"): "Reworded requirement."}
    )

    plan = stored_plan()
    assert issues_by_key(plan)["FR-1"].parent_plan_key == plan.roots[0].plan_key
    assert [child.plan_key for child in plan.children_of("FR-1")] != []
    assert "↳" in next(line for line in shown["markdown"] if "accept card payments" in line)

    # The next run renders the stored edit, still indented under its parent.
    shown, _ = render_review_panel(monkeypatch, plan)
    edited_line = next(line for line in shown["markdown"] if "Reworded requirement." in line)
    assert "↳" in edited_line
    assert "&nbsp;" in edited_line


def test_every_issue_at_every_depth_gets_review_controls(monkeypatch):
    plan = a_plan()

    shown, _ = render_review_panel(monkeypatch, plan)

    for issue in plan.issues:
        assert any(issue.plan_key in header for header in shown["expander"]), issue.plan_key


def test_no_hierarchy_name_is_invented_by_the_review_ui(monkeypatch):
    shown, offered = render_review_panel(monkeypatch, a_plan())

    rendered = " ".join(text for texts in shown.values() for text in texts)
    rendered += " ".join(str(entry["labels"]) for entry in offered)
    assert "Initiative" in rendered and "Deliverable" in rendered and "Step" in rendered
    for invented in ("Epic", "Story", "Sub-task", "Subtask"):
        assert invented not in rendered, invented


# --- Issue-type changes stay at the issue's own level ----------------------

def test_only_types_at_the_same_level_and_subtask_flag_are_compatible():
    metadata = a_metadata(CONTAINER, ITEM, SIBLING, SUBTASK, BLOCKED_SIBLING)
    item_issue = issues_by_key(a_plan())["FR-1"]

    names = [t.name for t in compatible_issue_types(item_issue, metadata)]

    assert names == ["Deliverable", "Chore"]
    assert "Initiative" not in names, "a different level would move the issue"
    assert "Step" not in names, "a subtask type is a different place in the hierarchy"
    assert "Blocked thing" not in names, "an unplannable type is not offered"


def test_a_subtask_issue_is_only_offered_subtask_types():
    sibling_subtask = a_type("31", "Chore step", level=-1, subtask=True)
    metadata = a_metadata(CONTAINER, ITEM, SUBTASK, sibling_subtask)
    subtask_issue = a_plan().children_of("FR-1")[0]

    names = [t.name for t in compatible_issue_types(subtask_issue, metadata)]

    assert names == ["Step", "Chore step"]


def test_a_type_the_project_never_reported_makes_nothing_compatible():
    """
    With no reported flag to match against, guessing one would invent hierarchy.
    """
    issue = PlannedIssue(plan_key="X", summary="Y", issue_type_id="not-reported")

    assert compatible_issue_types(issue, FULL_METADATA) == ()


def test_changing_to_a_compatible_type_copies_its_id_name_and_level():
    metadata = a_metadata(CONTAINER, ITEM, SIBLING, SUBTASK)

    plan = set_planned_issue_type(a_plan(), "FR-1", SIBLING, metadata)
    issue = issues_by_key(plan)["FR-1"]

    assert (issue.issue_type_id, issue.issue_type_name) == (SIBLING.id, SIBLING.name)
    assert issue.hierarchy_level == SIBLING.hierarchy_level
    assert issue.parent_plan_key == plan.roots[0].plan_key, "the tree did not move"


@pytest.mark.parametrize(
    "rejected",
    [CONTAINER, SUBTASK, BLOCKED_SIBLING, a_type("99", "Never reported", level=0)],
    ids=["higher-level", "subtask-type", "unplannable", "not-in-metadata"],
)
def test_an_incompatible_type_is_refused(rejected):
    metadata = a_metadata(CONTAINER, ITEM, SUBTASK, BLOCKED_SIBLING)
    plan = a_plan()

    assert set_planned_issue_type(plan, "FR-1", rejected, metadata) == plan


def test_the_ui_offers_only_compatible_types_and_applies_the_choice(monkeypatch):
    metadata = a_metadata(CONTAINER, ITEM, SIBLING, SUBTASK)

    _, offered = render_review_panel(
        monkeypatch,
        a_plan(),
        metadata=metadata,
        values={wkey("FR-1", "type"): SIBLING.id},
    )

    picker = next(entry for entry in offered if entry["key"] == wkey("FR-1", "type"))
    assert picker["labels"] == ["Deliverable", "Chore"]
    assert issues_by_key(stored_plan())["FR-1"].issue_type_name == "Chore"


def test_no_type_picker_is_offered_when_the_project_reports_no_alternative(monkeypatch):
    _, offered = render_review_panel(monkeypatch, a_plan(), metadata=FULL_METADATA)

    assert offered == [], "a picker with one option is a choice the project does not have"


# --- Validate parent/child relationships after edits ----------------------

def test_a_freshly_generated_plan_validates_clean():
    assert validate_work_plan(a_plan(), FULL_METADATA, PROJECT) == ()


def test_a_selected_child_whose_parent_is_deselected_is_reported():
    plan = update_planned_issue(a_plan(), "BRD", selected=False)

    errors = validate_work_plan(plan, FULL_METADATA, PROJECT)

    assert any("FR-1" in error and "parent" in error for error in errors), errors


def test_deselecting_a_leaf_and_keeping_its_parent_is_fine():
    """NFR-1 has no subtask, so dropping it leaves nothing stranded."""
    plan = update_planned_issue(a_plan(), "NFR-1", selected=False)

    assert validate_work_plan(plan, FULL_METADATA, PROJECT) == ()


def test_deselecting_an_issue_that_still_has_a_selected_child_is_reported():
    """
    FR-1 carries a subtask. Dropping the parent while keeping the child would leave a
    selected issue whose parent nothing would create.
    """
    plan = update_planned_issue(a_plan(), "FR-1", selected=False)

    errors = validate_work_plan(plan, FULL_METADATA, PROJECT)

    assert any("FR-1-A1" in error and "FR-1" in error for error in errors), errors


def test_deselecting_a_parent_and_its_child_together_is_fine():
    plan = update_planned_issue(a_plan(), "FR-1", selected=False)
    plan = update_planned_issue(plan, "FR-1-A1", selected=False)

    assert validate_work_plan(plan, FULL_METADATA, PROJECT) == ()


def test_an_issue_naming_a_missing_parent_is_reported():
    """
    Deletion cascades so this should be unreachable through the UI, which is exactly
    why it is worth checking: it is the invariant the cascade exists to hold.
    """
    plan = a_plan()
    orphaned = JiraWorkPlan(
        project_identifier=plan.project_identifier,
        project_label=plan.project_label,
        issues=tuple(issue for issue in plan.issues if issue.plan_key != "FR-1"),
    )

    errors = validate_work_plan(orphaned, FULL_METADATA, PROJECT)

    assert any("not in this plan" in error for error in errors), errors


@pytest.mark.parametrize(
    "summary, expected",
    [
        ("   ", "empty summary"),
        ("Two\nlines", "line break"),
        ("x" * (MAX_SUMMARY_LENGTH + 1), "longer than"),
    ],
    ids=["empty", "line-break", "too-long"],
)
def test_a_summary_jira_would_refuse_is_reported(summary, expected):
    plan = update_planned_issue(a_plan(), "FR-1", summary=summary)

    errors = validate_work_plan(plan, FULL_METADATA, PROJECT)

    assert any("FR-1" in error and expected in error for error in errors), errors


def test_a_missing_description_is_reported_only_where_this_project_requires_one():
    requiring = a_metadata(
        CONTAINER,
        a_type("20", "Deliverable", level=0, fields=(DESCRIPTION_REQUIRED,)),
        SUBTASK,
    )
    plan = update_planned_issue(a_plan(), "FR-1", description="  ")

    assert any(
        "FR-1" in error and "description" in error
        for error in validate_work_plan(plan, requiring, PROJECT)
    )
    # The same edit is fine where Jira fills the field in by itself.
    defaulted = a_metadata(
        CONTAINER,
        a_type("20", "Deliverable", level=0, fields=(DESCRIPTION_DEFAULTED,)),
        SUBTASK,
    )
    assert validate_work_plan(plan, defaulted, PROJECT) == ()


@pytest.mark.parametrize(
    "metadata",
    [
        # The type the plan already uses, now failing the required-field check.
        a_metadata(CONTAINER, a_type("20", "Deliverable", level=0, fields=BLOCKING_FIELDS), SUBTASK),
        a_metadata(CONTAINER, a_type("20", "Deliverable", level=0, fields=(), known=False), SUBTASK),
        a_metadata(CONTAINER, SUBTASK),
    ],
    ids=["blocked", "unchecked", "no-longer-reported"],
)
def test_an_unsupported_issue_type_is_reported(metadata):
    errors = validate_work_plan(a_plan(), metadata, PROJECT)

    assert any("unsupported issue type" in error for error in errors), errors


def test_a_deselected_issue_does_not_block_on_its_own_fields():
    plan = update_planned_issue(a_plan(), "FR-2", summary="", selected=False)

    assert all("FR-2" not in error for error in validate_work_plan(plan, FULL_METADATA, PROJECT))


def test_a_plan_with_nothing_selected_is_reported():
    plan = a_plan()
    for issue in plan.issues:
        plan = update_planned_issue(plan, issue.plan_key, selected=False)

    assert any("No issue is selected" in error for error in validate_work_plan(plan, FULL_METADATA, PROJECT))


def test_metadata_read_for_another_project_is_reported():
    other = a_metadata(CONTAINER, ITEM, project=JiraProject(id="99999", key="OTHER", name="Other"))

    errors = validate_work_plan(a_plan(), other, PROJECT)

    assert any("not for" in error for error in errors), errors


def test_a_plan_built_for_another_project_is_reported():
    elsewhere = JiraProject(id="99999", key="OTHER", name="Other")

    errors = validate_work_plan(a_plan(), FULL_METADATA, elsewhere)

    assert any("not for" in error for error in errors), errors


def test_validation_problems_are_shown_to_the_reviewer(monkeypatch):
    shown, _ = render_review_panel(
        monkeypatch, a_plan(), values={wkey("FR-1", "summary"): "   "}
    )

    assert any("FR-1" in message and "empty summary" in message for message in shown["error"])


def test_a_clean_plan_shows_no_validation_error(monkeypatch):
    shown, _ = render_review_panel(monkeypatch, a_plan())

    assert shown["error"] == []


# --- Reviewing a plan creates nothing -------------------------------------

def test_the_review_functions_have_no_way_to_reach_jira():
    source = inspect.getsource(jira_processor)

    assert "import requests" not in source
    assert not hasattr(jira_processor, "requests")
    for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
        assert verb not in source
    for review in (update_planned_issue, delete_planned_issue, set_planned_issue_type, validate_work_plan):
        parameters = inspect.signature(review).parameters
        assert "access_token" not in parameters, review.__name__
        assert "tokens" not in parameters, review.__name__
    # The panel still takes no service and no token, so it cannot be authorized.
    assert list(inspect.signature(main._render_jira_work_plan_panel).parameters) == [
        "project",
        "metadata",
        "scope",
    ]


@pytest.mark.parametrize(
    "action",
    [
        {"values": {wkey("FR-1", "summary"): "Edited."}},
        {"values": {wkey("FR-1", "selected"): False}},
        {"clicked": (wkey("FR-1", "delete"),)},
    ],
    ids=["edit", "deselect", "delete"],
)
def test_no_review_action_sends_a_request(monkeypatch, action):
    """The strongest form of "creates nothing": every HTTP verb is a test failure."""
    for verb in ("get", "post", "put", "patch", "delete"):
        monkeypatch.setattr(
            jira_service.requests,
            verb,
            lambda *a, **k: pytest.fail("reviewing a work plan must send no request"),
            raising=False,
        )

    render_review_panel(monkeypatch, a_plan(), **action)


def test_the_review_ui_offers_no_creation_action(monkeypatch):
    """JIRA-006 stops at review. Creation is a later, explicit ticket."""
    shown, _ = render_review_panel(monkeypatch, a_plan())

    rendered = " ".join(text for texts in shown.values() for text in texts).lower()
    for later_ticket in ("create selected issues", "issue key", "created in jira:"):
        assert later_ticket not in rendered, later_ticket
    assert any("nothing" in text.lower() for text in shown["success"] + shown["caption"])


# --- Review state does not outlive the plan it describes -------------------

def test_regenerating_the_plan_drops_stale_editor_state(monkeypatch):
    st.session_state[wkey("FR-1", "summary")] = "typed against the previous plan"
    st.session_state[main.BRD_SESSION_KEY] = a_brd()

    monkeypatch.setattr(main.st, "checkbox", lambda label, value=False, **k: value)
    monkeypatch.setattr(main.st, "text_input", lambda label, value="", **k: value)
    monkeypatch.setattr(main.st, "text_area", lambda label, value="", **k: value)
    monkeypatch.setattr(
        main.st, "selectbox", lambda label, options=(), index=0, **k: list(options)[index]
    )
    monkeypatch.setattr(
        main.st, "button", lambda label, **k: k.get("key") == "generate_jira_work_plan"
    )
    main._render_jira_work_plan_panel(PROJECT, FULL_METADATA, SCOPE)

    assert wkey("FR-1", "summary") not in st.session_state


@pytest.mark.parametrize(
    "clear",
    [
        lambda: main._disconnect(jira_service.JiraService()),
        lambda: main._clear_jira_project_state(jira_service.JiraService()),
        lambda: main._store_brd(a_brd(project_title="A different meeting")),
    ],
    ids=["disconnect", "project-change", "new-brd"],
)
def test_every_path_that_invalidates_the_plan_also_clears_editor_state(clear):
    st.session_state[main._skey("jira", "plan")] = a_plan()
    st.session_state[wkey("FR-1", "summary")] = "stale"

    clear()

    assert main._skey("jira", "plan") not in st.session_state
    assert wkey("FR-1", "summary") not in st.session_state
