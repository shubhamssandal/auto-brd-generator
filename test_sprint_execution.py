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
    def __init__(self, sprint_id="SPRINT1", sprint_name="Sprint 1", issues=None):
        self.sprint_id = sprint_id
        self.sprint_name = sprint_name
        self.issues = issues or []


class SimpleLifecycle:
    def __init__(self, implementation_plan):
        self.implementation_plan = implementation_plan
        self.sprint_executions = []


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        orig = os.getcwd()
        os.chdir(tmp)
        yield tmp
        os.chdir(orig)


def test_sprint_execution_single_story_success(workspace):
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="Task", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", summary="Task", story_id="S1", rationale="R", is_selected=True)])
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
    ])
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
    assert res.blocked_stories == 1
    assert res.failed_stories == 1
    assert res.overall_status == "Blocked"


def test_sprint_runner_class(workspace):
    plan = ImplementationPlan(stories=[
        Story(story_id="S1", title="T", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))
    ])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)])
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
    sprint = SimpleSprintPlan(issues=[])
    lifecycle = SimpleLifecycle(plan)
    res = execute_sprint(sprint, lifecycle, workspace_root=workspace)
    assert res.total_stories == 0
    assert res.overall_status == "Not Started"


def test_sprint_execution_story_not_found(workspace):
    plan = ImplementationPlan(stories=[Story(story_id="S2", title="T", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)])
    lifecycle = SimpleLifecycle(plan)
    res = execute_sprint(sprint, lifecycle, workspace_root=workspace)
    assert res.total_stories == 0
    assert res.not_started_stories == 1


def test_sprint_execution_human_approval_required(workspace):
    plan = ImplementationPlan(stories=[Story(story_id="S1", title="T", user_story="U", acceptance_criteria=("A",), tasks=(TechnicalTask(task_id="T1", title="T"),), component_ids=("c1",), feature_ids=("f1",))])
    sprint = SimpleSprintPlan(issues=[Mock(issue_key="I1", story_id="S1", is_selected=True)])
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
    ])
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
    ])
    lifecycle = SimpleLifecycle(plan)

    def se(**kw):
        sid = kw['story'].story_id
        if sid == "S1":
            return AICodingAgentResult(story_id=sid, files_changed=[CodeChange("ok.py", "created")], evidence_generated=True)
        return AICodingAgentResult(story_id=sid, files_changed=[CodeChange("fail.py", "created")], fix_attempts=3, blocked=True, evidence_generated=True)

    with patch('sprint_execution.run_ai_coding_agent', side_effect=se):
        res = execute_sprint(sprint, lifecycle, workspace_root=workspace)

    assert res.completed_stories == 1
    assert res.failed_stories == 1
    assert any("ok.py" == f.file_path for f in res.files_changed)
    assert any("fail.py" == f.file_path for f in res.files_changed)
