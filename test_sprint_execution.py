"""
Canonical test module for sprint-level coding execution.
"""
import json
import os
import tempfile
from unittest.mock import Mock, patch
import pytest
from coding_agent import CodeChange, AICodingAgentResult
from sprint_execution import (
    execute_sprint,
    run_sprint_and_collect_results,
    SprintExecutionRunner,
    SprintExecutionResult,
    approve_and_complete_sprint,
)
from implementation_plan_models import Story, ImplementationPlan, TechnicalTask
from test_case_models import TestSuite, TestCase, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL


class SimpleSprintPlan:
    def __init__(self, sprint_id="SPRINT1", sprint_name="Sprint 1", issues=None, approved=False):
        self.sprint_id = sprint_id
        self.sprint_name = sprint_name
        self.issues = issues or []
        self.approved = approved


class SimpleLifecycle:
    def __init__(self, implementation_plan):
        self.implementation_plan = implementation_plan
        self.sprint_executions = []
        self.story_execution_outcomes = {}
        self.last_sprint_execution_result = None


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        orig = os.getcwd()
        os.chdir(tmp)
        yield tmp
        os.chdir(orig)


def test_sprint_execution_blocked_without_sprint_approval(workspace):
    """Sprint execution must not start until the user explicitly approves the sprint."""
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    # sprint is NOT approved
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)], approved=False)
    lifecycle = SimpleLifecycle(plan)

    with patch('sprint_execution.run_ai_coding_agent') as coding_agent_spy:
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    # Sprint execution must not begin at all.
    assert coding_agent_spy.call_count == 0, (
        "Coding agent must not be called when sprint is unapproved"
    )
    assert res.total_stories == 0
    assert res.overall_status == "Not Started"
    assert res.human_approval_required is True
    assert any("not approved" in b.lower() for b in res.blockers)


def test_sprint_execution_works_after_sprint_approval(workspace):
    """Sprint execution proceeds normally after sprint is approved."""
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)], approved=True)
    lifecycle = SimpleLifecycle(plan)

    mock_resp = Mock()
    mock_resp.text = json.dumps({"files_to_create": [{"path": "g.py", "content": "x=1"}]})
    client = Mock()
    client.models.generate_content.return_value = mock_resp

    with patch('coding_agent.subprocess.run') as mock_sub, patch('coding_agent.genai.Client', return_value=client):
        mock_sub.return_value = Mock(returncode=0, stdout="", stderr="")
        res = execute_sprint(sprint, lifecycle, client=client, workspace_root=workspace)

    assert res.completed_stories == 1
    assert res.overall_status == "Completed"


def test_sprint_execution_single_story_success(workspace):
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", summary="Task", story_id="S1", rationale="R", is_selected=True)], approved=True)
    lifecycle = SimpleLifecycle(plan)

    mock_resp = Mock()
    mock_resp.text = json.dumps({"files_to_create": [{"path": "g.py", "content": "x=1"}]})
    client = Mock()
    client.models.generate_content.return_value = mock_resp

    with patch('coding_agent.subprocess.run') as mock_sub, patch('coding_agent.genai.Client', return_value=client):
        mock_sub.return_value = Mock(returncode=0, stdout="", stderr="")
        res = execute_sprint(sprint, lifecycle, client=client, workspace_root=workspace)

    assert res.completed_stories == 1
    assert res.overall_status == "Completed"
    assert res.human_approval_required is True
    assert len(res.files_changed) == 1


def test_sprint_execution_multiple_stories_different_outcomes(workspace):
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Ok", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",)),
        Story(story_id="S2", title="Blocked", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T2", title="T"),), component_ids=("c2",), feature_ids=("f2",)),
        Story(story_id="S3", title="Fail", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T3", title="T"),), component_ids=("c3",), feature_ids=("f3",)),
    ])
    sprint = SimpleSprintPlan(issues=[
        Mock(issue_key="I1", summary="Ok", story_id="S1", is_selected=True),
        Mock(issue_key="I2", summary="Blocked", story_id="S2", is_selected=True),
        Mock(issue_key="I3", summary="Fail", story_id="S3", is_selected=True),
    ], approved=True)
    lifecycle = SimpleLifecycle(plan)

    def side_effect(*args, **kwargs):
        story = kwargs.get('story')
        sid = story.story_id if story else "S1"
        if sid == "S1":
            return AICodingAgentResult(story_id=sid, files_changed=[CodeChange("f1.py", "created")], fix_attempts=0, blocked=False, evidence_generated=True)
        elif sid == "S2":
            return AICodingAgentResult(story_id=sid, blocked=True, blocked_reason="Not ready")
        else:
            return AICodingAgentResult(story_id=sid, files_changed=[CodeChange("f3.py", "created")], fix_attempts=3, blocked=True, blocked_reason="Failed", evidence_generated=True)

    with patch('sprint_execution.run_ai_coding_agent', side_effect=side_effect):
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    assert res.total_stories == 3
    assert res.completed_stories == 1
    assert res.blocked_stories == 2  # S2 and S3 both blocked (S3 blocked after retries)
    assert res.failed_stories == 0  # No separate "failed" category now; blocked includes failed-after-retries
    assert res.overall_status == "Blocked"


def test_sprint_runner_class(workspace):
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="T", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)], approved=True)
    lifecycle = SimpleLifecycle(plan)

    runner = SprintExecutionRunner(workspace_root=workspace)
    client = Mock()
    client.models.generate_content.return_value = Mock(text=json.dumps({"files_to_create": [{"path": "a.py", "content": "pass"}]}))

    with patch('coding_agent.subprocess.run') as mock_sub:
        mock_sub.return_value = Mock(returncode=0)
        res = runner.run_sprint(sprint, lifecycle, client=client)

    assert res.overall_status == "Completed"
    runner.approve_sprint(res)
    assert res.approved is True


def test_sprint_execution_no_stories(workspace):
    plan = ImplementationPlan(stories=[])
    sprint = SimpleSprintPlan(issues=[], approved=True)
    lifecycle = SimpleLifecycle(plan)
    res = execute_sprint(sprint, lifecycle, workspace_root=workspace)
    assert res.total_stories == 0
    assert res.overall_status == "Not Started"


def test_sprint_execution_story_not_found(workspace):
    plan = ImplementationPlan(stories=[Story(story_id="S2", title="T", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)], approved=True)
    lifecycle = SimpleLifecycle(plan)
    res = execute_sprint(sprint, lifecycle, workspace_root=workspace)
    assert res.total_stories == 0
    assert res.not_started_stories == 1


def test_sprint_execution_human_approval_required(workspace):
    plan = ImplementationPlan(stories=[Story(story_id="S1", title="T", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)], approved=True)
    lifecycle = SimpleLifecycle(plan)

    mock_resp = Mock()
    mock_resp.text = json.dumps({"files_to_create": [{"path": "g.py", "content": "x=1"}]})
    client = Mock()
    client.models.generate_content.return_value = mock_resp

    with patch('coding_agent.subprocess.run') as mock_sub, patch('coding_agent.genai.Client', return_value=client):
        mock_sub.return_value = Mock(returncode=0, stdout="", stderr="")
        res = execute_sprint(sprint, lifecycle, client=client, workspace_root=workspace)

    assert res.human_approval_required is True
    assert res.approved is False

    approved = approve_and_complete_sprint(res, sprint, lifecycle)
    assert approved.approved is True
    assert approved.overall_status == "Completed"


def test_sprint_execution_preserves_individual_results(workspace):
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="T1", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",)),
        Story(story_id="S2", title="T2", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T2", title="T"),), component_ids=("c2",), feature_ids=("f2",)),
    ])
    sprint = SimpleSprintPlan(issues=[
        Mock(issue_key="I1", story_id="S1", is_selected=True),
        Mock(issue_key="I2", story_id="S2", is_selected=True),
    ], approved=True)
    lifecycle = SimpleLifecycle(plan)

    with patch('sprint_execution.run_ai_coding_agent', side_effect=lambda **kw: AICodingAgentResult(story_id=kw['story'].story_id, files_changed=[CodeChange(f"out_{kw['story'].story_id}.py", "created")], evidence_generated=True)):
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    assert len(res.files_changed) == 2
    assert res.completed_stories == 2


def test_sprint_execution_failed_story_does_not_corrupt_others(workspace):
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="T1", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",)),
        Story(story_id="S2", title="T2", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T2", title="T"),), component_ids=("c2",), feature_ids=("f2",)),
    ])
    sprint = SimpleSprintPlan(issues=[
        Mock(issue_key="I1", story_id="S1", is_selected=True),
        Mock(issue_key="I2", story_id="S2", is_selected=True),
    ], approved=True)
    lifecycle = SimpleLifecycle(plan)

    def se(**kw):
        sid = kw['story'].story_id
        if sid == "S1":
            return AICodingAgentResult(story_id=sid, files_changed=[CodeChange("ok.py", "created")], evidence_generated=True)
        return AICodingAgentResult(story_id=sid, files_changed=[CodeChange("fail.py", "created")], fix_attempts=3, blocked=True, evidence_generated=True)

    with patch('sprint_execution.run_ai_coding_agent', side_effect=se):
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    # S1 completed, S2 blocked
    assert res.completed_stories == 1
    assert res.blocked_stories == 1
    # Files from both stories are preserved
    assert any("ok.py" == f.file_path for f in res.files_changed)
    assert any("fail.py" == f.file_path for f in res.files_changed)


def test_jira_story_mapping_survives_execution(workspace):
    """Verify that the story_id <-> Jira issue_key mapping is preserved in delivery mapping."""
    from implementation_plan_jira import DeliveryMapping, DeliveryLink

    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="T1", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",)),
    ])
    sprint = SimpleSprintPlan(issues=[
        Mock(issue_key="JIRA-101", story_id="S1", is_selected=True),
    ], approved=True)
    lifecycle = SimpleLifecycle(plan)

    # Pre-populate lifecycle with a delivery mapping that has the real issue key
    lifecycle.delivery_mapping = DeliveryMapping(
        project_identifier="TEST",
        project_label="Test Project",
        links=(
            DeliveryLink(
                plan_item_id="S1",
                issue_key="JIRA-101",
                issue_type_name="Story",
                summary="Story 1",
                work_type="Story",
                feature_ids=(),
                component_ids=(),
            ),
        ),
        notes=(),
    )

    mock_resp = Mock()
    mock_resp.text = json.dumps({"files_to_create": [{"path": "g.py", "content": "x=1"}]})
    client = Mock()
    client.models.generate_content.return_value = mock_resp

    with patch('coding_agent.subprocess.run') as mock_sub, patch('coding_agent.genai.Client', return_value=client):
        mock_sub.return_value = Mock(returncode=0, stdout="", stderr="")
        res = execute_sprint(sprint, lifecycle, client=client, workspace_root=workspace)

    # Verify the delivery mapping in the sprint execution preserves the real issue key
    assert len(lifecycle.sprint_executions) > 0
    sprint_mapping = lifecycle.sprint_executions[-1]
    story_link = next((l for l in sprint_mapping.links if l.plan_item_id == "S1"), None)
    assert story_link is not None
    assert story_link.issue_key == "JIRA-101"


def test_code_result_remains_associated_with_story(workspace):
    """Verify that AI coding result files are associated with the correct story_id."""
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="T1", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",)),
    ])
    sprint = SimpleSprintPlan(issues=[
        Mock(issue_key="I1", story_id="S1", is_selected=True),
    ], approved=True)
    lifecycle = SimpleLifecycle(plan)

    with patch('sprint_execution.run_ai_coding_agent', side_effect=lambda **kw: AICodingAgentResult(story_id=kw['story'].story_id, files_changed=[CodeChange(f"story_{kw['story'].story_id}_out.py", "created")], evidence_generated=True)):
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    assert len(res.files_changed) == 1
    assert res.files_changed[0].file_path == "story_S1_out.py"
    # And the story_id is stored in execution outcomes
    assert "S1" in lifecycle.story_execution_outcomes
    assert lifecycle.story_execution_outcomes["S1"]["completed"] is True


def test_test_evidence_remains_associated_with_story(workspace):
    """Verify test execution evidence is preserved per story."""
    from test_case_models import TestSuite, TestCase, TEST_EXECUTION_PASS

    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="T1", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",)),
    ])
    sprint = SimpleSprintPlan(issues=[
        Mock(issue_key="I1", story_id="S1", is_selected=True),
    ], approved=True)
    lifecycle = SimpleLifecycle(plan)

    # Provide test suite with evidence for the story
    test_suite = TestSuite(story_id="S1", test_cases=[
        TestCase(test_id="TC-1", story_reference="S1", scenario="s", preconditions="p", steps="st", expected_result="e", priority="High", test_type="Functional", execution_status=TEST_EXECUTION_PASS, actual_result="ok"),
    ])

    with patch('sprint_execution.run_ai_coding_agent', side_effect=lambda **kw: AICodingAgentResult(story_id=kw['story'].story_id, files_changed=[CodeChange("f.py", "created")], test_suites=[test_suite], evidence_generated=True)):
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    # Verify test_suites are stored in the result and have the correct story_id
    assert len(res.test_suites) > 0
    assert any(ts.story_id == "S1" for ts in res.test_suites)


def test_failed_blocked_missing_evidence_prevents_completion():
    """Test that sprint completion is evidence-driven and prevents false completion.

    This test verifies that sprint completion does NOT falsely report success when
    required delivery evidence is failed/blocked/missing, using the actual
    sprint_completion implementation.
    """
    from sprint_completion import complete_sprint
    from lifecycle_models import ProjectLifecycle, StageState
    from sprint_models import SprintPlan, SprintIssue
    from implementation_plan_models import ImplementationPlan, Story, TechnicalTask
    from test_case_models import TestSuite, TestCase, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL, TEST_EXECUTION_BLOCKED, TEST_EXECUTION_NOT_RUN

    # Create a lifecycle with implementation plan
    plan = ImplementationPlan(stories=[
        Story(
            story_id="S1",
            title="Test Story",
            user_story="U",
            acceptance_criteria=("A",),
            tasks=(TechnicalTask(task_id="T1", title="T"),),
            component_ids=("c1",),
            feature_ids=("f1",)
        )
    ])
    lifecycle = ProjectLifecycle(
        implementation_plan=plan,
        stages={"implementation_plan": StageState(status="Approved")}
    )

    # Create a sprint plan with one issue
    sprint = SprintPlan(
        sprint_name="Sprint 1",
        issues=[SprintIssue(issue_key="JIRA-101", story_id="S1")]
    )

    # Create test cases showing failed/blocked/missing evidence
    test_suites = [
        TestSuite(
            story_id="S1",
            test_cases=[
                TestCase(
                    test_id="TC-1",
                    story_reference="S1",
                    scenario="s",
                    preconditions="p",
                    steps="st",
                    expected_result="e",
                    priority="High",
                    test_type="Functional",
                    execution_status=TEST_EXECUTION_FAIL
                ),
                TestCase(
                    test_id="TC-2",
                    story_reference="S1",
                    scenario="s2",
                    preconditions="p2",
                    steps="st2",
                    expected_result="e2",
                    priority="Medium",
                    test_type="Functional",
                    execution_status=TEST_EXECUTION_BLOCKED
                ),
            ]
        )
    ]

    # Complete the sprint with failing test evidence
    completion = complete_sprint(lifecycle, sprint, test_suites)

    # Verify sprint is NOT completed when tests fail/block
    assert completion.overall_status == "Blocked"
    assert not completion.approved
    assert len(completion.remaining_backlog) == 1
    assert completion.remaining_backlog[0].story_id == "S1"
    assert not completion.story_completions[0].is_completed


def test_successful_evidence_allows_completion():
    """Test that sprint completion passes when all evidence is successful."""
    from sprint_completion import complete_sprint
    from lifecycle_models import ProjectLifecycle, StageState
    from sprint_models import SprintPlan, SprintIssue
    from implementation_plan_models import ImplementationPlan, Story, TechnicalTask
    from test_case_models import TestSuite, TestCase, TEST_EXECUTION_PASS

    # Create a lifecycle with implementation plan
    plan = ImplementationPlan(stories=[
        Story(
            story_id="S2",
            title="Successful Story",
            user_story="U",
            acceptance_criteria=("A",),
            tasks=(TechnicalTask(task_id="T2", title="T"),),
            component_ids=("c2",),
            feature_ids=("f2",)
        )
    ])
    lifecycle = ProjectLifecycle(
        implementation_plan=plan,
        stages={"implementation_plan": StageState(status="Approved")}
    )

    # Create a sprint plan with one issue
    sprint = SprintPlan(
        sprint_name="Sprint 2",
        issues=[SprintIssue(issue_key="JIRA-102", story_id="S2")]
    )

    # Create test cases showing passing evidence
    test_suites = [
        TestSuite(
            story_id="S2",
            test_cases=[
                TestCase(
                    test_id="TC-3",
                    story_reference="S2",
                    scenario="s3",
                    preconditions="p3",
                    steps="st3",
                    expected_result="e3",
                    priority="High",
                    test_type="Functional",
                    execution_status=TEST_EXECUTION_PASS
                ),
            ]
        )
    ]

    # Complete the sprint with passing test evidence
    completion = complete_sprint(lifecycle, sprint, test_suites)

    # Verify sprint is completed when all evidence passes
    assert completion.overall_status == "Completed"
    assert not completion.approved
    assert len(completion.remaining_backlog) == 0
    assert completion.story_completions[0].is_completed
    assert completion.story_completions[0].passed_count == 1


def test_existing_successful_behavior_still_passes(workspace):
    """Verify the canonical successful path still works."""
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", summary="Task", story_id="S1", is_selected=True)], approved=True)
    lifecycle = SimpleLifecycle(plan)

    mock_resp = Mock()
    mock_resp.text = json.dumps({"files_to_create": [{"path": "g.py", "content": "x=1"}]})
    client = Mock()
    client.models.generate_content.return_value = mock_resp

    with patch('coding_agent.subprocess.run') as mock_sub, patch('coding_agent.genai.Client', return_value=client):
        mock_sub.return_value = Mock(returncode=0, stdout="", stderr="")
        res = execute_sprint(sprint, lifecycle, client=client, workspace_root=workspace)

    assert res.completed_stories == 1
    assert res.overall_status == "Completed"
    assert len(res.files_changed) == 1
    # Per-story outcome is recorded
    assert lifecycle.story_execution_outcomes["S1"]["completed"] is True


def test_sprint_execution_passes_context_and_model_to_coding_agent(workspace):
    """Test that sprint execution passes PRD, architecture, implementation plan, and model_name to the coding agent."""
    # Create a simple implementation plan with one story
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    # Sprint with one issue
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)], approved=True)
    # Lifecycle with PRD, architecture, implementation plan, and model_name
    lifecycle = SimpleLifecycle(plan)
    lifecycle.prd = "PRD content"
    lifecycle.architecture = "Architecture content"
    lifecycle.implementation_plan = plan
    lifecycle.model_name = "test-model-123"

    # Mock the coding agent to capture arguments
    captured_args = {}
    def capture_args(**kwargs):
        captured_args.update(kwargs)
        return AICodingAgentResult(
            story_id=kwargs.get('story').story_id if kwargs.get('story') else "S1",
            files_changed=[],
            test_suites=[],
            fix_attempts=0,
            blocked=False,
            evidence_generated=True
        )

    with patch('sprint_execution.run_ai_coding_agent', side_effect=capture_args):
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    # Verify the coding agent was called
    assert captured_args, "run_ai_coding_agent was not called"
    # Verify the story was passed
    assert 'story' in captured_args
    assert captured_args['story'].story_id == "S1"
    # Verify PRD, architecture, implementation plan, and model_name were passed
    assert captured_args.get('prd_data') == "PRD content"
    assert captured_args.get('architecture_data') == "Architecture content"
    assert captured_args.get('implementation_plan') == plan
    assert captured_args.get('model_name') == "test-model-123"
    # Verify the sprint execution succeeded
    assert res.completed_stories == 1
    assert res.overall_status == "Completed"
