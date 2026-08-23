"""
Jira planner tests: the AI grouping layer between an approved BRD and a work plan.

Three things run through the whole file.

**No model is ever called.** ``generate_work_plan`` takes the model call as an
injected ``str -> str`` callable, so every test here hands it fixed text and asserts
what the deterministic passes do with it. The autouse fixture in ``conftest.py``
blanks ``main.CLIENT`` as well, so nothing can slip through to a live model.

**No hierarchy is assumed.** The project used here reports levels 3, 2, 0 and -1,
named *Programme / Stream / Work package / Step* -- non-contiguous levels and names
Jira's defaults never use. Anything that keyed off "Epic" or off levels being
adjacent integers would fail every test below.

**The planner may not invent requirements.** Most of the file is about what happens
to a model answer that names something the BRD does not contain, points a parent at
nothing, or proposes work that traces to no requirement at all: it is repaired or
dropped, and the reason is reported in the plan's notes.

Fixtures and the small BRD come from ``test_jira_work_plan`` rather than being built
again -- this is the same mapping seen one layer up.
"""

import ast
import inspect
import json

import pytest
import streamlit as st

import jira_planner
import jira_service
import main
from brd_models import ActionItem, Requirement
from jira_models import MAX_SUMMARY_LENGTH
from jira_planner import (
    MAX_PLANNED_ISSUES,
    MAX_RATIONALE_LENGTH,
    PlannerResponseError,
    action_item_index,
    assemble_plan,
    build_planning_prompt,
    generate_work_plan,
    parse_planner_response,
)
from jira_processor import build_work_plan, creation_order, plannable_levels, validate_work_plan
from test_jira_work_plan import (  # noqa: F401 -- fixtures are used by pytest, not by name
    PROJECT,
    SCOPE,
    a_brd,
    a_metadata,
    a_type,
    clean_session,
    forbid_write_requests,
    issues_by_key,
    render_plan_panel,
)

# A four-rung project. The reported levels are 3, 2, 0, -1: deliberately not
# consecutive, so nothing can treat "the level above" as arithmetic on Jira's number
# rather than as the next rung this project actually offers.
PROGRAMME = a_type("10", "Programme", level=3)
STREAM = a_type("20", "Stream", level=2)
PACKAGE = a_type("30", "Work package", level=0)
STEP = a_type("40", "Step", level=-1, subtask=True)

FULL = a_metadata(PROGRAMME, STREAM, PACKAGE, STEP)
TYPE_NAMES = {"Programme", "Stream", "Work package", "Step"}


@pytest.fixture(autouse=True)
def no_http_at_all(monkeypatch):
    """Planning reads no Jira endpoint. ``forbid_write_requests`` covers the rest."""
    monkeypatch.setattr(
        jira_service.requests,
        "get",
        lambda *a, **k: pytest.fail("planning must not send a get request"),
        raising=False,
    )


def a_complex_brd(**overrides):
    """
    A BRD with several distinct areas of work, which is where a 1:1 mapping fails.

    Seven requirements that clearly group into ordering, stock, promotions and
    quality, and three action items whose text names no requirement id at all -- the
    linking a planner has to do by meaning rather than by string match.
    """
    data = dict(
        project_title="Distributor Portal Rollout",
        business_objectives=["Move distributor ordering off the phone."],
        functional_requirements=[
            Requirement("FR-1", "The system shall let distributors place orders online.", "they phone the orders in today"),
            Requirement("FR-2", "The system shall show live stock availability per depot.", "stock visibility is the big one"),
            Requirement("FR-3", "The system shall apply trade promotion discounts at order time.", "promo pricing is all manual"),
            Requirement("FR-4", "The system shall let a depot manager approve an order above the credit limit.", "credit limit sign-off"),
            Requirement("FR-5", "The system shall notify a distributor when an order ships.", "they keep chasing us for status"),
        ],
        non_functional_requirements=[
            Requirement("NFR-1", "The order screen must load within 2 seconds.", "it has to feel fast"),
            Requirement("NFR-2", "The portal must support both regional languages.", "half of them read the regional language"),
        ],
        acceptance_criteria=["An order placed online is accepted end to end."],
        action_items=[
            ActionItem(item="Confirm the promotion master data owner with trade marketing", owner="Priya"),
            ActionItem(item="Get the depot stock feed schedule from the warehouse team", owner="Ravi"),
            ActionItem(item="Book the quarterly distributor council room", owner="Anil"),
        ],
    )
    data.update(overrides)
    return a_brd(**data)


def a_grouped_response():
    """
    A well-formed model answer for ``a_complex_brd`` against ``FULL``.

    One programme, four streams, five work packages and one step. The programme names
    no requirement of its own -- a grouping item inherits from what it contains, which
    is what the assembly is expected to work out.
    """
    return {
        "items": [
            {"id": "P1", "summary": "Distributor Portal Rollout", "issue_type": "Programme",
             "level": 0, "parent": "",
             "rationale": "Groups the requirements approved in this BRD."},
            {"id": "S1", "summary": "Online ordering", "issue_type": "Stream", "level": 1,
             "parent": "P1", "requirement_ids": ["FR-1", "FR-4"]},
            {"id": "S2", "summary": "Depot stock visibility", "issue_type": "Stream", "level": 1,
             "parent": "P1", "requirement_ids": ["FR-2"], "action_item_ids": ["AI-2"]},
            {"id": "S3", "summary": "Trade promotion pricing", "issue_type": "Stream", "level": 1,
             "parent": "P1", "requirement_ids": ["FR-3"], "action_item_ids": ["AI-1"]},
            {"id": "S4", "summary": "Portal quality attributes", "issue_type": "Stream",
             "level": 1, "parent": "P1", "requirement_ids": ["NFR-1", "NFR-2"]},
            {"id": "W1", "summary": "Online order placement screen", "issue_type": "Work package",
             "level": 2, "parent": "S1", "requirement_ids": ["FR-1"],
             "acceptance_criteria": ["An order placed online is accepted end to end."],
             "rationale": "Implements FR-1."},
            {"id": "W2", "summary": "Credit limit approval", "issue_type": "Work package",
             "level": 2, "parent": "S1", "requirement_ids": ["FR-4"]},
            {"id": "W3", "summary": "Shipment notification", "issue_type": "Work package",
             "level": 2, "parent": "S1", "requirement_ids": ["FR-5"]},
            {"id": "W4", "summary": "Depot stock feed", "issue_type": "Work package", "level": 2,
             "parent": "S2", "requirement_ids": ["FR-2"], "action_item_ids": ["AI-2"]},
            {"id": "W5", "summary": "Promotion discount calculation", "issue_type": "Work package",
             "level": 2, "parent": "S3", "requirement_ids": ["FR-3"]},
            {"id": "T1", "summary": "Confirm the promotion master data owner",
             "issue_type": "Step", "level": 3, "parent": "W5", "action_item_ids": ["AI-1"]},
        ]
    }


def a_model(payload, record=None):
    """
    A stand-in for the model: returns fixed text, and records the prompt it was given.

    ``payload`` may be a JSON-serialisable object or the raw text to return, so a test
    can describe either a well-formed answer or a malformed one.
    """

    def generate(prompt):
        if record is not None:
            record.append(prompt)
        return payload if isinstance(payload, str) else json.dumps(payload)

    return generate


def plan_from(payload, brd_data=None, metadata=FULL, project=PROJECT, record=None):
    """The plan a model answer produces, with nothing else stubbed."""
    return generate_work_plan(
        brd_data if brd_data is not None else a_complex_brd(),
        project,
        metadata,
        generate=a_model(payload, record),
    )


def one_item(**overrides):
    """A single-item response, for tests about one specific malformation."""
    item = dict(id="W1", summary="Online order placement screen",
                issue_type="Work package", level=2, requirement_ids=["FR-1"])
    item.update(overrides)
    return {"items": [item]}


def note_matching(plan, fragment):
    """The first note containing ``fragment``, or ``""`` -- so a failure prints why."""
    return next((note for note in plan.notes if fragment in note), "")


# --- Grouping: the whole reason this layer exists ---------------------------

def test_requirements_are_grouped_into_the_full_depth_the_project_supports():
    plan = plan_from(a_grouped_response())

    by_key = issues_by_key(plan)
    assert [issue.plan_key for issue in plan.roots] == ["P1"]
    assert [issue.plan_key for issue in plan.children_of("P1")] == ["S1", "S2", "S3", "S4"]
    assert [issue.plan_key for issue in plan.children_of("S1")] == ["W1", "W2", "W3"]
    assert [issue.plan_key for issue in plan.children_of("W5")] == ["T1"]
    assert by_key["P1"].issue_type_name == "Programme"
    assert by_key["S1"].issue_type_name == "Stream"
    assert by_key["W1"].issue_type_name == "Work package"
    assert by_key["T1"].issue_type_name == "Step"
    # Four distinct rungs used, which the one-to-one mapping cannot reach.
    assert len({issue.hierarchy_level for issue in plan.issues}) == 4


def test_grouping_beats_the_one_to_one_mapping_on_the_same_brd():
    """
    The complaint this layer answers: a complex BRD used to yield one container and
    one item per requirement, two levels deep whatever the project offered.
    """
    brd_data = a_complex_brd()
    deterministic = build_work_plan(brd_data, PROJECT, FULL)
    grouped = plan_from(a_grouped_response(), brd_data=brd_data)

    assert len({issue.hierarchy_level for issue in deterministic.issues}) == 2
    assert len({issue.hierarchy_level for issue in grouped.issues}) == 4
    assert len(grouped.children_of("P1")) > len(deterministic.roots)


def test_no_issue_type_outside_the_ones_the_project_reported_is_used():
    plan = plan_from(a_grouped_response())

    assert {issue.issue_type_name for issue in plan.issues} <= TYPE_NAMES
    assert {issue.issue_type_id for issue in plan.issues} <= {"10", "20", "30", "40"}


def test_a_grouping_issue_inherits_the_traceability_of_what_it_contains():
    plan = plan_from(a_grouped_response())
    by_key = issues_by_key(plan)

    # The model gave the programme no requirement of its own. The order is the union
    # walked child-first, which keeps a plan's traceability stable between renders.
    assert by_key["P1"].source_requirement_ids == (
        "FR-1", "FR-4", "FR-5", "FR-2", "FR-3", "NFR-1", "NFR-2",
    )
    # FR-5 was named on a work package, not on the stream above it.
    assert "FR-5" in by_key["S1"].source_requirement_ids
    assert by_key["P1"].source_action_item_ids == ("AI-2", "AI-1")
    assert all(issue.is_traceable for issue in plan.issues)


def test_the_plan_passes_the_existing_validation_and_creation_ordering():
    plan = plan_from(a_grouped_response())

    assert validate_work_plan(plan, FULL, PROJECT) == ()
    ordered = [issue.plan_key for issue in creation_order(plan)]
    assert ordered.index("P1") < ordered.index("S1") < ordered.index("W1")
    assert ordered.index("W5") < ordered.index("T1")
    assert len(ordered) == len(plan.issues)


def test_the_notes_say_how_much_of_the_brd_was_grouped_and_how_deep():
    plan = plan_from(a_grouped_response())

    assert "grouped 7 of this BRD's 7 requirement(s) into 11 proposed issue(s)" in note_matching(
        plan, "grouped 7"
    )
    assert "4 of this project's 4 usable hierarchy level(s)" in note_matching(plan, "usable")


def test_acceptance_criteria_and_rationale_reach_the_issue():
    by_key = issues_by_key(plan_from(a_grouped_response()))

    assert by_key["W1"].acceptance_criteria == (
        "An order placed online is accepted end to end.",
    )
    assert by_key["W1"].rationale == "Implements FR-1."
    assert by_key["W2"].rationale == ""


def test_the_description_names_the_requirements_and_action_items_it_traces_to():
    by_key = issues_by_key(plan_from(a_grouped_response()))

    assert "FR-1" in by_key["W1"].description
    assert "Traces to BRD requirements" in by_key["W1"].description
    assert "Confirm the promotion master data owner with trade marketing" in by_key["T1"].description
    # The verbatim quote travels with an issue that restates exactly one requirement.
    assert "they phone the orders in today" in by_key["W1"].description
    assert "Not yet created in Jira." in by_key["W1"].description


# --- Traceability is enforced after the model answers, not asked for --------

def test_an_item_the_brd_does_not_support_is_dropped_and_reported():
    """The one thing this app must not do is invent a requirement."""
    payload = a_grouped_response()
    payload["items"].append(
        {"id": "X9", "summary": "Add a loyalty points engine", "issue_type": "Work package",
         "level": 2, "parent": "S1"}
    )

    plan = plan_from(payload)

    assert "X9" not in issues_by_key(plan)
    assert "Add a loyalty points engine" in note_matching(plan, "traced to no BRD requirement")
    assert "Nothing is proposed that the BRD does not state." in note_matching(
        plan, "traced to no BRD requirement"
    )


def test_a_requirement_id_that_is_not_in_the_brd_is_removed_and_reported():
    plan = plan_from(one_item(requirement_ids=["FR-1", "FR-99"]))

    assert issues_by_key(plan)["W1"].source_requirement_ids == ("FR-1",)
    assert "FR-99" in note_matching(plan, "not in this")


def test_an_action_item_id_that_is_not_in_the_brd_is_removed_and_reported():
    plan = plan_from(one_item(action_item_ids=["AI-1", "AI-77"]))

    assert issues_by_key(plan)["W1"].source_action_item_ids == ("AI-1",)
    assert "AI-77" in note_matching(plan, "AI-77")


def test_an_item_justified_only_by_an_action_item_is_kept():
    plan = plan_from(one_item(requirement_ids=[], action_item_ids=["AI-2"]))

    issue = issues_by_key(plan)["W1"]
    assert issue.source_requirement_ids == ()
    assert issue.source_action_item_ids == ("AI-2",)
    assert issue.is_traceable


def test_an_action_item_is_linked_without_its_text_naming_a_requirement_id():
    """
    ``AI-1`` reads "Confirm the promotion master data owner with trade marketing" and
    contains no requirement id, so the string-matching mapping could never place it.
    """
    brd_data = a_complex_brd()
    deterministic = build_work_plan(brd_data, PROJECT, FULL)
    grouped = plan_from(a_grouped_response(), brd_data=brd_data)

    assert not any(
        "promotion master data owner" in issue.summary for issue in deterministic.issues
    )
    linked = [issue for issue in grouped.issues if "AI-1" in issue.source_action_item_ids]
    # The step it was linked to, plus every ancestor of that step.
    assert [issue.plan_key for issue in linked] == ["P1", "S3", "W5", "T1"]


def test_an_action_item_that_belongs_nowhere_is_reported_rather_than_attached():
    plan = plan_from(a_grouped_response())

    note = note_matching(plan, "could not be linked")
    assert "Book the quarterly distributor council room" in note
    assert "listed here for review" in note
    assert not any("council room" in issue.summary for issue in plan.issues)


def test_requirements_no_proposed_issue_covers_are_reported():
    payload = a_grouped_response()
    payload["items"] = [item for item in payload["items"] if item["id"] not in ("S4", "W3")]

    plan = plan_from(payload)

    note = note_matching(plan, "not covered by any proposed issue")
    assert "NFR-1" in note and "NFR-2" in note and "FR-5" in note


def test_a_long_rationale_is_capped_so_no_reasoning_can_be_dumped_into_it():
    plan = plan_from(one_item(rationale="because " * 200))

    assert len(issues_by_key(plan)["W1"].rationale) <= MAX_RATIONALE_LENGTH


def test_a_summary_is_forced_onto_one_line_within_jira_s_limit():
    plan = plan_from(one_item(summary="Online ordering\nsecond line " + "x" * 400))

    summary = issues_by_key(plan)["W1"].summary
    assert "\n" not in summary
    assert len(summary) <= MAX_SUMMARY_LENGTH


# --- Only the hierarchy the selected project actually supports --------------

def test_a_project_with_one_usable_level_flattens_the_proposal():
    plan = plan_from(a_grouped_response(), metadata=a_metadata(PACKAGE))

    assert len(plannable_levels(a_metadata(PACKAGE))) == 1
    assert all(issue.parent_plan_key == "" for issue in plan.issues)
    assert len(plan.roots) == len(plan.issues)
    assert {issue.issue_type_name for issue in plan.issues} == {"Work package"}
    assert "supports 1 hierarchy level(s)" in note_matching(plan, "hierarchy level(s), so")
    # Nothing is left to group, so the grouping item now traces to nothing itself.
    assert "P1" not in issues_by_key(plan)
    assert validate_work_plan(plan, a_metadata(PACKAGE), PROJECT) == ()


def test_a_project_with_two_usable_levels_repoints_deeper_items_upwards():
    metadata = a_metadata(PROGRAMME, PACKAGE)

    plan = plan_from(a_grouped_response(), metadata=metadata)

    by_key = issues_by_key(plan)
    assert [issue.plan_key for issue in plan.roots] == ["P1"]
    assert all(
        issue.parent_plan_key == "P1" for issue in plan.issues if issue.plan_key != "P1"
    )
    assert by_key["T1"].issue_type_name == "Work package"
    assert "supports 2 hierarchy level(s)" in note_matching(plan, "hierarchy level(s), so")
    assert validate_work_plan(plan, metadata, PROJECT) == ()


def test_a_project_offering_only_subtask_types_never_reaches_the_model():
    """
    A subtask cannot be created without a parent, so there is no level to propose at.
    ``build_work_plan`` already says so; asking a model to plan into nothing would only
    spend a call.
    """
    metadata = a_metadata(STEP)
    record = []

    plan = plan_from(a_grouped_response(), metadata=metadata, record=record)

    assert record == []
    assert plan.is_empty
    assert "is a subtask type, and a subtask needs a parent" in note_matching(
        plan, "subtask needs a parent"
    )


def test_an_issue_type_the_project_does_not_offer_is_placed_by_its_parent():
    payload = {
        "items": [
            {"id": "P1", "summary": "Distributor Portal Rollout", "issue_type": "Programme",
             "level": 0, "requirement_ids": ["FR-1"]},
            {"id": "E1", "summary": "Ordering epic", "issue_type": "Epic", "parent": "P1",
             "requirement_ids": ["FR-1"]},
        ]
    }

    plan = plan_from(payload)

    by_key = issues_by_key(plan)
    assert by_key["E1"].issue_type_name == "Stream"
    assert by_key["E1"].parent_plan_key == "P1"
    assert "named no issue type this project offers" in note_matching(plan, "no issue type this")


def test_an_item_at_a_subtask_level_with_no_parent_is_raised_to_stand_alone():
    payload = {
        "items": [
            {"id": "W1", "summary": "Online order placement screen",
             "issue_type": "Work package", "level": 2, "requirement_ids": ["FR-1"]},
            {"id": "T9", "summary": "Chase the stock feed schedule", "issue_type": "Step",
             "level": 3, "parent": "", "action_item_ids": ["AI-2"]},
        ]
    }

    plan = plan_from(payload)

    raised = issues_by_key(plan)["T9"]
    assert raised.issue_type_name == "Work package"
    assert raised.parent_plan_key == ""
    assert "raised to the deepest level that can stand alone" in note_matching(plan, "subtask")
    assert validate_work_plan(plan, FULL, PROJECT) == ()


# --- A malformed answer is repaired, and the repair is reported -------------

def test_a_parent_that_names_nothing_in_the_plan_is_dropped_and_reported():
    plan = plan_from(one_item(parent="does-not-exist"))

    assert issues_by_key(plan)["W1"].parent_plan_key == ""
    assert "named a parent that is not in this plan" in note_matching(plan, "named a parent")
    assert validate_work_plan(plan, FULL, PROJECT) == ()


def test_a_parent_cycle_is_broken_and_reported():
    payload = {
        "items": [
            {"id": "A", "summary": "Online ordering", "issue_type": "Stream", "level": 1,
             "parent": "B", "requirement_ids": ["FR-1"]},
            {"id": "B", "summary": "Depot stock visibility", "issue_type": "Stream", "level": 1,
             "parent": "A", "requirement_ids": ["FR-2"]},
            {"id": "C", "summary": "Trade promotion pricing", "issue_type": "Stream", "level": 1,
             "parent": "C", "requirement_ids": ["FR-3"]},
        ]
    }

    plan = plan_from(payload)

    by_key = issues_by_key(plan)
    assert set(by_key) == {"A", "B", "C"}
    assert by_key["A"].parent_plan_key == ""
    assert by_key["C"].parent_plan_key == ""
    assert "formed a parent cycle" in note_matching(plan, "cycle")
    assert validate_work_plan(plan, FULL, PROJECT) == ()
    assert len(creation_order(plan)) == 3


def test_a_repeated_id_is_dropped_because_every_parent_reference_to_it_is_ambiguous():
    payload = {
        "items": [
            {"id": "W1", "summary": "Online order placement screen",
             "issue_type": "Work package", "level": 2, "requirement_ids": ["FR-1"]},
            {"id": "W1", "summary": "Something else entirely", "issue_type": "Work package",
             "level": 2, "requirement_ids": ["FR-2"]},
        ]
    }

    plan = plan_from(payload)

    assert [issue.summary for issue in plan.issues] == ["Online order placement screen"]
    assert "repeated an id already used" in note_matching(plan, "repeated an id")


def test_plan_keys_are_safe_to_use_as_widget_keys():
    plan = plan_from(one_item(id="W1 / stage 2: ordering [draft]"))

    key = plan.issues[0].plan_key
    assert "/" not in key and ":" not in key and "[" not in key
    assert key.strip() == key and key


def test_json_wrapped_in_prose_and_a_code_fence_is_still_read():
    raw = "Here is the plan you asked for:\n```json\n{}\n```\nHope that helps!".format(
        json.dumps(a_grouped_response())
    )

    plan = plan_from(raw)

    assert len(plan.issues) == 11
    assert [issue.plan_key for issue in plan.roots] == ["P1"]


def test_a_row_missing_an_id_or_a_summary_is_skipped():
    items = parse_planner_response(
        json.dumps(
            {"items": [
                {"summary": "no id here"},
                {"id": "W2"},
                {"id": "W3", "summary": "Shipment notification"},
            ]}
        )
    )

    assert [item.identifier for item in items] == ["W3"]


def test_a_bare_list_of_items_is_accepted():
    items = parse_planner_response('[{"id": "W1", "summary": "Online order placement"}]')

    assert [item.identifier for item in items] == ["W1"]


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "I could not plan this project.", "{}", '{"items": []}',
     '{"items": [{"summary": "no id"}]}', "null"],
    ids=["empty", "blank", "prose", "empty-object", "no-items", "unusable-item", "null"],
)
def test_a_response_with_nothing_usable_in_it_raises(raw):
    with pytest.raises(PlannerResponseError):
        parse_planner_response(raw)


def test_more_items_than_the_cap_are_truncated_and_reported():
    payload = {
        "items": [
            {"id": "W{}".format(index), "summary": "Work item {}".format(index),
             "issue_type": "Work package", "level": 2, "requirement_ids": ["FR-1"]}
            for index in range(MAX_PLANNED_ISSUES + 3)
        ]
    }

    plan = plan_from(payload)

    assert len(plan.issues) == MAX_PLANNED_ISSUES
    assert "proposed {} items".format(MAX_PLANNED_ISSUES + 3) in note_matching(plan, "proposed ")


# --- Every failure ends in the deterministic plan, never in an empty screen -

def test_no_configured_model_falls_back_to_the_one_to_one_mapping():
    brd_data = a_complex_brd()

    plan = generate_work_plan(brd_data, PROJECT, FULL, generate=None)

    assert [issue.plan_key for issue in plan.issues] == [
        issue.plan_key for issue in build_work_plan(brd_data, PROJECT, FULL).issues
    ]
    assert "The AI planner was not available" in plan.notes[0]
    assert "GEMINI_API_KEY" in plan.notes[0]


def test_a_failing_model_call_falls_back_and_leaks_nothing_from_the_error():
    """
    Only the exception type is reported. A client's message can carry request context,
    which has no business in something rendered on a review screen.
    """
    marker = "url=https://example.invalid/v1?key=SHOULD-NOT-APPEAR"

    def generate(prompt):
        raise RuntimeError(marker)

    plan = generate_work_plan(a_complex_brd(), PROJECT, FULL, generate=generate)

    assert not plan.is_empty
    assert "could not be reached (RuntimeError)" in plan.notes[0]
    assert not any(marker in note for note in plan.notes)
    assert not any("SHOULD-NOT-APPEAR" in note for note in plan.notes)


def test_an_unreadable_response_falls_back_and_says_so():
    plan = plan_from("I am unable to help with that request.")

    assert not plan.is_empty
    assert "could not be read as JSON" in plan.notes[0]
    assert "maps each BRD requirement to one issue instead" in plan.notes[0]


def test_a_response_holding_no_work_item_falls_back_and_says_so():
    plan = plan_from({"items": []})

    assert not plan.is_empty
    assert "held no work items" in plan.notes[0]
    assert "maps each BRD requirement to one issue instead" in plan.notes[0]


def test_a_response_where_nothing_survives_validation_falls_back_with_both_reasons():
    payload = {
        "items": [
            {"id": "X1", "summary": "Add a loyalty points engine",
             "issue_type": "Work package", "level": 2},
            {"id": "X2", "summary": "Replatform the ERP", "issue_type": "Work package",
             "level": 2, "requirement_ids": ["FR-404"]},
        ]
    }

    plan = plan_from(payload)

    assert not plan.is_empty
    assert any("nothing in it survived validation" in note for note in plan.notes)
    assert any("traced to no BRD requirement" in note for note in plan.notes)
    # The fallback plan is the deterministic one, not a half-built AI plan.
    assert "FR-1" in issues_by_key(plan)
    assert not any(issue.summary == "Replatform the ERP" for issue in plan.issues)


def test_a_plan_for_another_project_s_metadata_is_refused_before_the_model_is_called():
    record = []
    other = a_metadata(PROGRAMME, STREAM, project=PROJECT)
    other = type(other)(project_identifier="99999", issue_types=other.issue_types)

    plan = generate_work_plan(
        a_complex_brd(), PROJECT, other, generate=a_model(a_grouped_response(), record)
    )

    assert record == []
    assert plan.is_empty


def test_a_brd_with_no_confirmed_requirement_never_reaches_the_model():
    record = []
    empty = a_brd(functional_requirements=[], non_functional_requirements=[])

    plan = generate_work_plan(
        empty, PROJECT, FULL, generate=a_model(a_grouped_response(), record)
    )

    assert record == []
    assert plan.is_empty


# --- What the planner is given, and what it cannot reach --------------------

def _imported_modules(module) -> set:
    tree = ast.parse(inspect.getsource(module))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_the_planner_holds_no_client_no_credential_and_no_jira_access():
    """
    The model call is injected, so this module imports nothing that could reach the
    network or read a key -- which is also why every test in this file can run offline.
    """
    imported = _imported_modules(jira_planner)

    assert not imported & {
        "requests", "httpx", "urllib", "os", "google", "genai", "main", "streamlit",
        "jira_service", "jira_config", "dotenv",
    }
    for func in (generate_work_plan, assemble_plan, build_planning_prompt):
        taken = set(inspect.signature(func).parameters)
        assert not taken & {"access_token", "refresh_token", "token", "tokens", "api_key",
                            "service", "session", "site_id"}


def test_the_prompt_carries_the_approved_brd_and_this_project_s_own_levels():
    record = []
    plan_from(a_grouped_response(), record=record)
    prompt = record[0]

    for identifier in ("FR-1", "FR-5", "NFR-2", "AI-1", "AI-3"):
        assert identifier in prompt
    assert "The system shall show live stock availability per depot." in prompt
    assert "Book the quarterly distributor council room" in prompt
    for name in TYPE_NAMES:
        assert name in prompt
    assert "Engineering (ENG)" in prompt
    assert "an item here MUST have a parent" in prompt


def test_the_prompt_does_not_carry_the_transcript_the_brd_was_built_from():
    """
    The planner works from approved requirements. Handing it the raw quotes would let
    it plan from something the reviewer never approved.
    """
    record = []
    plan_from(a_grouped_response(), record=record)
    prompt = record[0]

    for quote in (
        "they phone the orders in today",
        "stock visibility is the big one",
        "promo pricing is all manual",
        "credit limit sign-off",
    ):
        assert quote not in prompt


def test_out_of_scope_items_are_named_in_the_prompt_as_out_of_scope():
    record = []
    brd_data = a_complex_brd(
        deferred_or_out_of_scope_items=["A distributor mobile app is out of scope for now."]
    )
    plan_from(a_grouped_response(), brd_data=brd_data, record=record)

    prompt = record[0]
    assert "Explicitly out of scope — plan NO work for these" in prompt
    assert "A distributor mobile app is out of scope for now." in prompt


def test_action_item_ids_follow_brd_position():
    index = action_item_index(a_complex_brd())

    assert list(index) == ["AI-1", "AI-2", "AI-3"]
    assert index["AI-2"].item == "Get the depot stock feed schedule from the warehouse team"


def test_an_action_item_with_no_text_is_not_given_an_id_but_does_not_shift_the_others():
    brd_data = a_complex_brd(
        action_items=[
            ActionItem(item="   "),
            ActionItem(item="Get the depot stock feed schedule from the warehouse team"),
        ]
    )

    index = action_item_index(brd_data)

    assert list(index) == ["AI-2"]


# --- Step 4 in the app ------------------------------------------------------

def test_step_four_uses_the_ai_planner_when_a_model_is_configured(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_complex_brd()
    monkeypatch.setattr(main, "_planner_generate", lambda: a_model(a_grouped_response()))

    shown = render_plan_panel(FULL, monkeypatch, clicked=True)

    plan = st.session_state[main._skey("jira", "plan")]
    assert [issue.plan_key for issue in plan.roots] == ["P1"]
    assert len(plan.children_of("P1")) == 4
    assert any("Nothing has been created in Jira" in text for text in shown["success"])


def test_step_four_still_works_with_no_model_configured(monkeypatch):
    """``conftest`` blanks ``main.CLIENT``, so this is the no-key path end to end."""
    st.session_state[main.BRD_SESSION_KEY] = a_complex_brd()

    shown = render_plan_panel(FULL, monkeypatch, clicked=True)

    plan = st.session_state[main._skey("jira", "plan")]
    assert not plan.is_empty
    assert "FR-1" in issues_by_key(plan)
    # A plan's notes are the panel's ``st.info`` messages.
    assert any("The AI planner was not available" in text for text in shown["info"])


def test_the_panel_says_the_plan_is_a_proposal_and_creates_nothing(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_complex_brd()
    monkeypatch.setattr(main, "_planner_generate", lambda: a_model(a_grouped_response()))

    shown = render_plan_panel(FULL, monkeypatch, clicked=True)

    assert any(
        "Proposal only — nothing has been created in Jira." in text
        for text in shown["caption"]
    )


def test_the_review_screen_shows_where_each_issue_came_from(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_complex_brd()
    monkeypatch.setattr(main, "_planner_generate", lambda: a_model(a_grouped_response()))

    shown = render_plan_panel(FULL, monkeypatch, clicked=True)

    text = "\n".join(shown["markdown"] + shown["caption"])
    assert "**Source BRD requirement(s):** `FR-1`" in text
    assert "**Linked action item(s) from the meeting**" in text
    # The id is what the plan stores; the reviewer is shown what it says.
    assert "`AI-1` Confirm the promotion master data owner with trade marketing" in text
    assert "**Why this issue:** Implements FR-1." in text
    assert "Proposed beneath S1 in this plan." in text


def test_the_review_screen_shows_an_action_item_id_alone_if_the_brd_was_replaced(monkeypatch):
    st.session_state[main.BRD_SESSION_KEY] = a_complex_brd()
    monkeypatch.setattr(main, "_planner_generate", lambda: a_model(a_grouped_response()))
    render_plan_panel(FULL, monkeypatch, clicked=True)

    # A BRD generated after the plan has different action items.
    st.session_state[main.BRD_SESSION_KEY] = a_brd(action_items=[])
    shown = render_plan_panel(FULL, monkeypatch, clicked=False)

    text = "\n".join(shown["markdown"])
    # The step's own summary still names the action item; the linked line no longer can.
    assert "- `AI-1`" in text
    assert "- `AI-1` Confirm the promotion master data owner" not in text


def test_generating_a_plan_through_the_ui_creates_nothing_in_jira(monkeypatch):
    """The autouse fixtures fail the test on any HTTP verb, including ``get``."""
    st.session_state[main.BRD_SESSION_KEY] = a_complex_brd()
    monkeypatch.setattr(main, "_planner_generate", lambda: a_model(a_grouped_response()))

    render_plan_panel(FULL, monkeypatch, clicked=True)

    assert main._skey("jira", "created") not in st.session_state
    assert main._skey("jira", "confirm_create") not in st.session_state
