"""
Phase 4: the approved PRD + architecture -> implementation plan, through explicit approval.

Four properties are pinned here, each one a rule rather than an implementation detail.

*Both approved artifacts are the gate.* A missing PRD, a missing architecture and an
unapproved architecture each block generation and say which input is missing, because work
decomposed against scope or a design nobody signed off would have to be re-decomposed.

*The hierarchy means something.* An epic groups, a story is an outcome with acceptance
criteria, a task is technical work. A story must trace to a PRD feature -- one that traces
to nothing is invented scope and is dropped -- while a task naming no component is kept,
because repository setup and release steps genuinely touch no single component.

*A plan is repaired deterministically, never re-prompted.* Unknown ids, self-references,
dangling dependencies, dependency cycles and out-of-vocabulary priorities are all fixed by
ordinary code, and every fix is reported in the notes.

*A failure degrades, it never fabricates.* An unreachable provider and a malformed response
both fall back to the deterministic per-feature skeleton, flagged as a fallback, and neither
one approves anything.

The model is never called: ``generate`` is injected as a plain callable, so a test supplies
the exact response text it wants to exercise.

The tests below the ``--- Session state ---`` marker are the only ones that touch
``main``; everything above it exercises the artifact and the generator, which hold no
Streamlit dependency at all.
"""

import json

import pytest

from architecture_models import BACKEND, MOBILE, WEB, ArchitectureData, Component
from implementation_plan_generator import (
    ImplementationPlanResponseError,
    MAX_TASKS_PER_STORY,
    assemble_implementation_plan,
    break_dependency_cycles,
    build_baseline_implementation_plan,
    build_implementation_plan_prompt,
    generate_implementation_plan,
    parse_implementation_plan_response,
)
from implementation_plan_models import (
    DEFAULT_PRIORITY,
    SUBTASK,
    TASK,
    ImplementationPlan,
    Story,
    TechnicalTask,
    architecture_component_ids,
    normalise_priority,
)
from lifecycle_models import (
    APPROVED,
    DRAFT,
    IMPLEMENTATION_PLAN,
    IMPLEMENTED_STAGES,
    NOT_STARTED,
    PENDING_REVIEW,
    lifecycle_from,
)
from prd_models import Feature, PRDData


# --- Fixtures as plain builders -------------------------------------------
#
# Built rather than fixtured so the pure-logic tests need no pytest machinery: this file's
# lower half needs Streamlit, its upper half needs nothing at all, and keeping the split
# clean is what makes the artifact layer testable on its own.


def _prd(count: int = 2) -> PRDData:
    """A PRD with ``count`` features, each carrying its own acceptance criteria."""
    features = tuple(
        Feature(
            feature_id="F-{}".format(index),
            name="Feature {}".format(index),
            summary="What feature {} does.".format(index),
            requirement_ids=("FR-{}".format(index),),
            acceptance_criteria=("Criterion {}a".format(index),),
        )
        for index in range(1, count + 1)
    )
    return PRDData(
        project_title="Test Product",
        overview="A product.",
        goals=("Ship it.",),
        features=features,
        source_brd_title="Test Product",
        source_requirement_ids=tuple("FR-{}".format(i) for i in range(1, count + 1)),
    )


def _architecture(feature_ids=("F-1", "F-2")) -> ArchitectureData:
    """An architecture with one component per layer, each realising one feature."""
    components = []
    for position, (layer, identifier) in enumerate(
        [(BACKEND, feature_ids[0]), (WEB, feature_ids[0]), (MOBILE, feature_ids[-1])],
        start=1,
    ):
        components.append(
            Component(
                component_id="C-{}".format(position),
                name="Component {}".format(position),
                layer=layer,
                responsibility="Owns something.",
                feature_ids=(identifier,),
            )
        )
    return ArchitectureData(
        project_title="Test Product",
        overview="Three components.",
        components=tuple(components),
        source_prd_title="Test Product",
        source_feature_ids=tuple(feature_ids),
    )


def _payload(**overrides) -> dict:
    """A well-formed model proposal, before any override a test wants."""
    payload = {
        "overview": "Build the backend first.",
        "epics": [
            {"key": "E1", "name": "Capability one", "goal": "Deliver one.", "feature_ids": ["F-1"]}
        ],
        "stories": [
            {
                "key": "S1",
                "epic": "E1",
                "title": "Do the first thing",
                "user_story": "As a user, I want the first thing so that I benefit.",
                "acceptance_criteria": ["It works"],
                "feature_ids": ["F-1"],
                "component_ids": ["C-1"],
                "priority": "High",
                "tasks": [
                    {
                        "title": "Add the table",
                        "work_type": "task",
                        "detail": "Migration and model.",
                        "component_ids": ["C-1"],
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _plan(payload=None, prd=None, architecture=None) -> ImplementationPlan:
    """A plan assembled from ``payload`` against the standard PRD and architecture."""
    return assemble_implementation_plan(
        payload if payload is not None else _payload(),
        prd if prd is not None else _prd(),
        architecture if architecture is not None else _architecture(),
    )


# --- The gate -------------------------------------------------------------


def test_no_prd_refuses_and_says_which_input_is_missing():
    plan = generate_implementation_plan(None, _architecture(), generate=lambda _p: "{}")
    assert plan.is_empty
    assert plan.epics == ()
    assert any("approved PRD" in note for note in plan.notes)


def test_prd_without_features_refuses():
    plan = generate_implementation_plan(PRDData(), _architecture(), generate=lambda _p: "{}")
    assert plan.is_empty
    assert any("approved PRD" in note for note in plan.notes)


def test_no_architecture_refuses_and_names_the_architecture():
    plan = generate_implementation_plan(_prd(), None, generate=lambda _p: "{}")
    assert plan.is_empty
    assert any("approved architecture" in note for note in plan.notes)


def test_architecture_without_components_refuses():
    plan = generate_implementation_plan(
        _prd(), ArchitectureData(project_title="Empty"), generate=lambda _p: "{}"
    )
    assert plan.is_empty
    assert any("approved architecture" in note for note in plan.notes)


def test_a_refusal_never_calls_the_model():
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return "{}"

    generate_implementation_plan(None, None, generate=generate)
    assert calls == []


# --- The hierarchy --------------------------------------------------------


def test_epic_story_task_hierarchy_is_preserved():
    plan = _plan()
    assert [epic.epic_id for epic in plan.epics] == ["E-1"]
    assert [story.story_id for story in plan.stories] == ["S-1"]
    assert plan.stories[0].epic_id == "E-1"
    assert plan.stories_for("E-1") == plan.stories
    assert [task.task_id for task in plan.stories[0].tasks] == ["S-1-T-1"]
    assert plan.task_count == 1
    assert plan.total_items == 3


def test_ids_are_minted_here_not_taken_from_the_model():
    plan = _plan(
        _payload(
            epics=[{"key": "whatever-the-model-called-it", "name": "Cap", "feature_ids": ["F-1"]}],
            stories=[
                {
                    "key": "MODEL-999",
                    "epic": "whatever-the-model-called-it",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task"}],
                }
            ],
        )
    )
    assert plan.epics[0].epic_id == "E-1"
    assert plan.stories[0].story_id == "S-1"
    assert plan.stories[0].epic_id == "E-1"


def test_a_subtask_is_recorded_as_a_subtask_and_anything_else_as_a_task():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [
                        {"title": "One", "work_type": "sub-task"},
                        {"title": "Two", "work_type": "task"},
                        {"title": "Three", "work_type": "something odd"},
                    ],
                }
            ]
        )
    )
    assert [task.work_type for task in plan.stories[0].tasks] == [SUBTASK, TASK, TASK]


def test_a_story_naming_an_unknown_epic_is_kept_but_left_ungrouped():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E-NOPE",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task"}],
                }
            ]
        )
    )
    assert len(plan.stories) == 1
    assert plan.stories[0].epic_id == ""
    assert plan.orphan_stories == plan.stories
    assert any("not in the plan" in note for note in plan.notes)


# --- Traceability ---------------------------------------------------------


def test_a_story_tracing_to_nothing_is_dropped_as_invented_scope():
    plan = _plan(
        _payload(
            stories=[
                {"epic": "E1", "title": "Invented work", "acceptance_criteria": ["ok"]},
                {
                    "epic": "E1",
                    "title": "Real work",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task"}],
                },
            ]
        )
    )
    assert [story.title for story in plan.stories] == ["Real work"]
    assert any("Invented work" in note for note in plan.notes)
    assert any("add it as a task" in note for note in plan.notes)


def test_an_unknown_feature_id_is_removed_and_reported():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1", "F-99"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task"}],
                }
            ]
        )
    )
    assert plan.stories[0].feature_ids == ("F-1",)
    assert any("F-99" in note and "not in the PRD" in note for note in plan.notes)


def test_a_feature_id_is_stored_in_the_prds_own_spelling():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["f-1 "],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task"}],
                }
            ]
        )
    )
    assert plan.stories[0].feature_ids == ("F-1",)


def test_an_unknown_component_id_is_removed_and_reported():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "component_ids": ["C-1", "C-404"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task", "component_ids": ["C-404"]}],
                }
            ]
        )
    )
    assert plan.stories[0].component_ids == ("C-1",)
    assert plan.stories[0].tasks[0].component_ids == ()
    assert any("C-404" in note and "not in the architecture" in note for note in plan.notes)


def test_a_task_naming_no_component_is_kept_as_cross_cutting():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "Set up CI"}, {"title": "Build it", "component_ids": ["C-1"]}],
                }
            ]
        )
    )
    assert [task.title for task in plan.stories[0].tasks] == ["Set up CI", "Build it"]
    assert plan.stories[0].tasks[0].component_ids == ()


def test_an_epic_that_named_no_feature_inherits_its_stories_coverage():
    plan = _plan(
        _payload(
            epics=[{"key": "E1", "name": "Cap", "goal": "Deliver."}],
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-2"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task"}],
                }
            ],
        )
    )
    assert plan.epics[0].feature_ids == ("F-2",)


def test_uncovered_features_and_components_are_reported_separately():
    plan = _plan()
    assert plan.covered_feature_ids == ("F-1",)
    assert plan.uncovered_feature_ids == ("F-2",)
    assert plan.covered_component_ids == ("C-1",)
    assert set(plan.uncovered_component_ids) == {"C-2", "C-3"}
    assert any("not delivered by any story" in note for note in plan.notes)
    assert any("not built by any story or task" in note for note in plan.notes)


def test_traceability_reaches_from_requirement_through_to_task():
    prd = _prd()
    plan = _plan(prd=prd)
    story = plan.stories[0]
    # BRD requirement -> PRD feature is the PRD's link; the plan picks it up from there.
    feature = next(item for item in prd.features if item.feature_id == story.feature_ids[0])
    assert feature.requirement_ids == ("FR-1",)
    assert story.tasks[0].component_ids == ("C-1",)
    assert "C-1" in architecture_component_ids(_architecture())


# --- Deterministic repair -------------------------------------------------


def test_a_self_dependency_is_removed_and_reported():
    plan = _plan(
        _payload(
            stories=[
                {
                    "key": "S1",
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "depends_on": ["S1"],
                    "tasks": [{"title": "A task"}],
                }
            ]
        )
    )
    assert plan.stories[0].depends_on == ()
    assert any("depended on themselves" in note for note in plan.notes)


def test_a_dependency_on_no_known_story_is_removed_and_reported():
    plan = _plan(
        _payload(
            stories=[
                {
                    "key": "S1",
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "depends_on": ["S-GHOST"],
                    "tasks": [{"title": "A task"}],
                }
            ]
        )
    )
    assert plan.stories[0].depends_on == ()
    assert any("matched no story" in note for note in plan.notes)


def test_a_forward_dependency_resolves_because_ids_are_matched_in_a_second_pass():
    plan = _plan(
        _payload(
            stories=[
                {
                    "key": "A",
                    "epic": "E1",
                    "title": "First declared",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "depends_on": ["B"],
                    "tasks": [{"title": "A task"}],
                },
                {
                    "key": "B",
                    "epic": "E1",
                    "title": "Declared later",
                    "feature_ids": ["F-2"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "A task"}],
                },
            ]
        )
    )
    assert plan.stories[0].depends_on == ("S-2",)
    assert plan.ordered_story_ids == ("S-2", "S-1")


def test_a_dependency_cycle_loses_only_the_closing_link():
    plan = _plan(
        _payload(
            stories=[
                {
                    "key": "A",
                    "epic": "E1",
                    "title": "A",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "depends_on": ["B"],
                    "tasks": [{"title": "t"}],
                },
                {
                    "key": "B",
                    "epic": "E1",
                    "title": "B",
                    "feature_ids": ["F-2"],
                    "acceptance_criteria": ["ok"],
                    "depends_on": ["A"],
                    "tasks": [{"title": "t"}],
                },
            ]
        )
    )
    edges = {story.story_id: story.depends_on for story in plan.stories}
    # One direction survives, the other does not: the work is startable again.
    assert sum(len(value) for value in edges.values()) == 1
    assert any("cycle" in note for note in plan.notes)
    assert set(plan.ordered_story_ids) == {"S-1", "S-2"}


def test_ordered_story_ids_places_every_story_after_its_prerequisites():
    plan = _plan(
        _payload(
            stories=[
                {
                    "key": "C",
                    "epic": "E1",
                    "title": "C",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "depends_on": ["B"],
                    "tasks": [{"title": "t"}],
                },
                {
                    "key": "B",
                    "epic": "E1",
                    "title": "B",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "depends_on": ["A"],
                    "tasks": [{"title": "t"}],
                },
                {
                    "key": "A",
                    "epic": "E1",
                    "title": "A",
                    "feature_ids": ["F-2"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [{"title": "t"}],
                },
            ]
        )
    )
    order = plan.ordered_story_ids
    assert len(order) == 3
    for story in plan.stories:
        for parent in story.depends_on:
            assert order.index(parent) < order.index(story.story_id)


def test_break_dependency_cycles_is_a_no_op_on_an_acyclic_plan():
    stories = [
        Story(story_id="S-1", title="one", feature_ids=("F-1",)),
        Story(story_id="S-2", title="two", feature_ids=("F-1",), depends_on=("S-1",)),
    ]
    repaired, dropped = break_dependency_cycles(stories)
    assert dropped == ()
    assert [story.depends_on for story in repaired] == [(), ("S-1",)]


def test_a_priority_outside_the_vocabulary_is_defaulted_and_reported():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "priority": "extremely urgent indeed",
                    "tasks": [{"title": "A task"}],
                }
            ]
        )
    )
    assert plan.stories[0].priority == DEFAULT_PRIORITY
    assert any("were not one of" in note for note in plan.notes)


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("High", "High"),
        ("high", "High"),
        ("P0", "Highest"),
        ("critical", "Highest"),
        ("nice to have", "Low"),
        ("Medium.", "Medium"),
        ("", None),
        ("banana", None),
    ],
)
def test_priority_normalisation(spoken, expected):
    assert normalise_priority(spoken) == expected


def test_too_many_tasks_on_one_story_are_trimmed_and_reported():
    plan = _plan(
        _payload(
            stories=[
                {
                    "epic": "E1",
                    "title": "A story",
                    "feature_ids": ["F-1"],
                    "acceptance_criteria": ["ok"],
                    "tasks": [
                        {"title": "Task {}".format(index)}
                        for index in range(MAX_TASKS_PER_STORY + 4)
                    ],
                }
            ]
        )
    )
    assert len(plan.stories[0].tasks) == MAX_TASKS_PER_STORY
    assert any("technical tasks" in note for note in plan.notes)


def test_a_story_with_no_criteria_or_tasks_is_kept_flagged_and_reported():
    plan = _plan(
        _payload(
            stories=[
                {"epic": "E1", "title": "Thin story", "feature_ids": ["F-1"]}
            ]
        )
    )
    story = plan.stories[0]
    assert not story.is_ready
    assert "no acceptance criteria" in story.readiness_gaps
    assert "no technical tasks" in story.readiness_gaps
    assert plan.unready_stories == (story,)
    assert any("no acceptance criteria" in note for note in plan.notes)
    assert any("no technical task" in note for note in plan.notes)


def test_a_complete_story_is_ready():
    story = _plan().stories[0]
    assert story.readiness_gaps == ()
    assert story.is_ready
    assert _plan().unready_stories == ()


def test_assembly_is_deterministic():
    first = _plan()
    second = _plan()
    assert first == second


# --- Reading the response -------------------------------------------------


def test_a_bare_list_is_read_as_the_story_list():
    assert parse_implementation_plan_response('[{"title": "A story"}]') == {
        "stories": [{"title": "A story"}]
    }


def test_a_nested_plan_object_is_unwrapped():
    payload = parse_implementation_plan_response(
        json.dumps({"implementation_plan": {"stories": [{"title": "x"}]}})
    )
    assert payload["stories"] == [{"title": "x"}]


def test_a_fenced_response_is_read():
    payload = parse_implementation_plan_response(
        "```json\n" + json.dumps(_payload()) + "\n```"
    )
    assert payload["epics"][0]["name"] == "Capability one"


def test_a_response_with_no_stories_or_epics_is_refused():
    with pytest.raises(ImplementationPlanResponseError):
        parse_implementation_plan_response(json.dumps({"overview": "words only"}))


def test_a_response_that_is_not_json_is_refused():
    with pytest.raises(ImplementationPlanResponseError):
        parse_implementation_plan_response("I am afraid I cannot help with that.")


def test_an_empty_response_is_refused():
    with pytest.raises(ImplementationPlanResponseError):
        parse_implementation_plan_response("")


# --- The fallback ---------------------------------------------------------


def test_no_model_falls_back_to_the_deterministic_skeleton():
    plan = generate_implementation_plan(_prd(), _architecture(), generate=None)
    assert plan.is_baseline
    assert not plan.is_empty
    assert any("GEMINI_API_KEY" in note for note in plan.notes)


def test_the_baseline_borrows_the_prds_acceptance_criteria_rather_than_inventing_any():
    plan = build_baseline_implementation_plan(_prd(), _architecture())
    assert plan.stories[0].acceptance_criteria == ("Criterion 1a",)
    assert plan.stories[0].feature_ids == ("F-1",)
    # One story per feature, one task per realising component.
    assert [story.story_id for story in plan.stories] == ["S-1", "S-2"]
    assert [task.component_ids for task in plan.stories[0].tasks] == [("C-1",), ("C-2",)]


def test_the_baseline_proposes_no_sequencing_it_cannot_justify():
    plan = build_baseline_implementation_plan(_prd(), _architecture())
    assert all(story.depends_on == () for story in plan.stories)
    assert all(story.estimate == "" for story in plan.stories)


def test_an_unreachable_model_falls_back_and_leaks_no_client_message():
    secret = "token=abc123 leaked from the client"

    def generate(_prompt):
        raise RuntimeError(secret)

    plan = generate_implementation_plan(_prd(), _architecture(), generate=generate)
    assert plan.is_baseline
    assert any("RuntimeError" in note for note in plan.notes)
    assert all(secret not in note for note in plan.notes)


def test_a_malformed_response_falls_back():
    plan = generate_implementation_plan(
        _prd(), _architecture(), generate=lambda _p: "not json at all"
    )
    assert plan.is_baseline
    assert not plan.is_empty


def test_a_response_whose_every_story_is_dropped_falls_back_with_both_sets_of_notes():
    payload = json.dumps({"stories": [{"title": "Untraceable"}]})
    plan = generate_implementation_plan(_prd(), _architecture(), generate=lambda _p: payload)
    assert plan.is_baseline
    assert not plan.is_empty
    assert any("Untraceable" in note for note in plan.notes)


def test_a_usable_response_is_not_a_baseline():
    payload = json.dumps(_payload())
    plan = generate_implementation_plan(_prd(), _architecture(), generate=lambda _p: payload)
    assert not plan.is_baseline
    assert plan.stories[0].title == "Do the first thing"


def test_generation_approves_nothing():
    payload = json.dumps(_payload())
    plan = generate_implementation_plan(_prd(), _architecture(), generate=lambda _p: payload)
    # There is no approval flag on the artifact at all: approval lives in the session, set
    # only by the approval control, so a generated plan cannot arrive pre-approved.
    assert not hasattr(plan, "approved")
    state = lifecycle_from(
        prd=_prd(),
        prd_approved=True,
        brd_approved=True,
        architecture=_architecture(),
        architecture_approved=True,
        implementation_plan=plan,
    ).state(IMPLEMENTATION_PLAN)
    assert state.status == PENDING_REVIEW


# --- The prompt -----------------------------------------------------------


def test_the_prompt_gives_the_model_the_ids_it_must_name():
    prompt = build_implementation_plan_prompt(_prd(), _architecture())
    for identifier in ("F-1", "F-2", "C-1", "C-2", "C-3"):
        assert identifier in prompt
    assert "Feature 1" in prompt
    assert "Component 1" in prompt


def test_the_prompt_states_the_hierarchy_rules():
    prompt = build_implementation_plan_prompt(_prd(), _architecture())
    assert "Do not force technical work into a user story" in prompt
    assert "must name at least one feature id" in prompt
    assert "Do not create circular dependencies" in prompt


def test_the_prompt_never_mentions_a_tracker_because_this_stage_writes_to_none():
    prompt = build_implementation_plan_prompt(_prd(), _architecture()).lower()
    assert "jira" not in prompt


def test_nothing_in_the_stage_is_domain_specific():
    """
    The engine must build a streaming product as readily as a shop.

    Pinned as a test rather than trusted, because a single example word in a prompt is how
    a domain-agnostic pipeline quietly becomes an e-commerce one.
    """
    prompt = build_implementation_plan_prompt(_prd(), _architecture()).lower()
    for word in ("ecommerce", "e-commerce", "catalogue", "checkout", "cart", "shopper"):
        assert word not in prompt

    import implementation_plan_generator as generator
    import implementation_plan_models as models

    for module in (generator, models):
        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read().lower()
        for word in ("ecommerce", "e-commerce", "checkout", "shopper"):
            assert word not in source, "{} mentions {}".format(module.__name__, word)


def test_a_streaming_domain_plans_exactly_as_an_unlabelled_one_does():
    prd = PRDData(
        project_title="Streaming Service",
        features=(
            Feature(
                feature_id="F-1",
                name="Continue watching",
                summary="Resume playback where the viewer left off.",
                requirement_ids=("FR-1",),
            ),
        ),
        source_requirement_ids=("FR-1",),
    )
    architecture = ArchitectureData(
        project_title="Streaming Service",
        components=(
            Component(
                component_id="C-1",
                name="Playback position service",
                layer=BACKEND,
                feature_ids=("F-1",),
            ),
        ),
        source_feature_ids=("F-1",),
    )
    payload = json.dumps(
        {
            "epics": [{"key": "E1", "name": "Playback", "feature_ids": ["F-1"]}],
            "stories": [
                {
                    "epic": "E1",
                    "title": "Resume playback",
                    "feature_ids": ["F-1"],
                    "component_ids": ["C-1"],
                    "acceptance_criteria": ["Playback resumes within 2s"],
                    "tasks": [{"title": "Store playback position", "component_ids": ["C-1"]}],
                }
            ],
        }
    )
    plan = generate_implementation_plan(prd, architecture, generate=lambda _p: payload)
    assert not plan.is_baseline
    assert plan.uncovered_feature_ids == ()
    assert plan.uncovered_component_ids == ()
    assert plan.stories[0].is_ready


# --- The lifecycle stage --------------------------------------------------


def _lifecycle(**overrides):
    """The lifecycle with every upstream approval granted unless a test says otherwise."""
    arguments = {
        "brd_approved": True,
        "prd": _prd(),
        "prd_approved": True,
        "architecture": _architecture(),
        "architecture_approved": True,
    }
    arguments.update(overrides)
    return lifecycle_from(**arguments)


def test_the_stage_is_reported_as_implemented():
    assert IMPLEMENTATION_PLAN in IMPLEMENTED_STAGES


def test_without_an_architecture_the_stage_is_not_started():
    state = _lifecycle(architecture=None, architecture_approved=False).state(IMPLEMENTATION_PLAN)
    assert state.status == NOT_STARTED
    assert "Generate an architecture first" in state.detail


def test_an_unapproved_architecture_blocks_the_stage():
    state = _lifecycle(architecture_approved=False).state(IMPLEMENTATION_PLAN)
    assert state.status == NOT_STARTED
    assert "pending review" in state.detail


def test_an_approved_architecture_with_no_plan_invites_generation():
    state = _lifecycle().state(IMPLEMENTATION_PLAN)
    assert state.status == NOT_STARTED
    assert "Generate an implementation plan" in state.detail


def test_a_generated_plan_is_pending_review_and_reports_its_size():
    state = _lifecycle(implementation_plan=_plan()).state(IMPLEMENTATION_PLAN)
    assert state.status == PENDING_REVIEW
    assert "1 epic(s)" in state.detail
    assert "Review, edit and approve" in state.detail


def test_a_baseline_plan_is_a_draft_and_says_it_is_the_fallback():
    plan = build_baseline_implementation_plan(_prd(), _architecture())
    state = _lifecycle(implementation_plan=plan).state(IMPLEMENTATION_PLAN)
    assert state.status == DRAFT
    assert "deterministic fallback" in state.detail


def test_an_approved_plan_is_approved():
    state = _lifecycle(
        implementation_plan=_plan(), implementation_plan_approved=True
    ).state(IMPLEMENTATION_PLAN)
    assert state.status == APPROVED


@pytest.mark.parametrize("revoked", ["brd_approved", "prd_approved", "architecture_approved"])
def test_revoking_any_upstream_approval_cascades_down_to_the_plan(revoked):
    state = _lifecycle(
        implementation_plan=_plan(),
        implementation_plan_approved=True,
        **{revoked: False}
    ).state(IMPLEMENTATION_PLAN)
    assert state.status == NOT_STARTED


def test_an_empty_plan_does_not_read_as_progress():
    state = _lifecycle(implementation_plan=ImplementationPlan()).state(IMPLEMENTATION_PLAN)
    assert state.status == NOT_STARTED


def test_the_plan_stage_does_not_disturb_the_delivery_stage():
    lifecycle = _lifecycle(implementation_plan=_plan(), implementation_plan_approved=True)
    # Approving a plan creates nothing in Jira, so the delivery stage has not moved.
    assert lifecycle.state("delivery_status").status == NOT_STARTED


# --- Session state --------------------------------------------------------
#
# Everything below here needs Streamlit, because it is about what the app holds between
# reruns rather than about the artifact.


def test_a_new_architecture_forgets_the_plan_its_approval_and_its_editors():
    import streamlit as st

    import main

    st.session_state.clear()
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = _plan()
    st.session_state[main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = True
    st.session_state[main._PLAN_WIDGET_PREFIX + "overview"] = "a leftover edit"

    main._clear_architecture_state()

    assert main.IMPLEMENTATION_PLAN_SESSION_KEY not in st.session_state
    assert main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY not in st.session_state
    assert main._PLAN_WIDGET_PREFIX + "overview" not in st.session_state
    st.session_state.clear()


def test_a_new_brd_forgets_the_plan_too_because_the_whole_chain_is_derived():
    import streamlit as st

    import main

    st.session_state.clear()
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = _plan()
    st.session_state[main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY] = True

    main._clear_prd_state()

    assert main.IMPLEMENTATION_PLAN_SESSION_KEY not in st.session_state
    assert main.IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY not in st.session_state
    st.session_state.clear()


def test_only_an_implementation_plan_is_read_back_from_the_session():
    import streamlit as st

    import main

    st.session_state.clear()
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = {"not": "a plan"}
    assert main._held_implementation_plan() is None
    plan = _plan()
    st.session_state[main.IMPLEMENTATION_PLAN_SESSION_KEY] = plan
    assert main._held_implementation_plan() == plan
    st.session_state.clear()


def test_the_stage_is_wired_into_the_workspace():
    import main

    assert hasattr(main, "_render_implementation_plan_stage")
    assert IMPLEMENTATION_PLAN in main.IMPLEMENTED_STAGES
