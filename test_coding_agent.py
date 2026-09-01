"""
Tests for the AI Coding Agent module.
"""
import json
import os
import tempfile
import shutil
from unittest.mock import Mock, patch
import pytest
from coding_agent import run_ai_coding_agent, CodeChange, AICodingAgentResult
from implementation_plan_models import Story, ImplementationPlan, TechnicalTask
from test_case_models import TestSuite, TestCase, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL


def test_run_ai_coding_agent_story_not_ready():
    """Test that agent returns blocked when story is not ready."""
    story = Story(
        story_id="S1",
        title="Test Story",
        user_story="As a user, I want to do something",
        acceptance_criteria=("Criterion 1",),  # Missing tasks and component_ids
        tasks=(),
        component_ids=()
    )

    result = run_ai_coding_agent(story)

    assert result.blocked == True
    assert "not ready" in result.blocked_reason
    assert result.story_id == "S1"


def test_code_change_dataclass():
    """Test CodeChange dataclass."""
    change = CodeChange(
        file_path="test.py",
        change_type="created",
        diff_summary="Created test file"
    )

    assert change.file_path == "test.py"
    assert change.change_type == "created"
    assert change.diff_summary == "Created test file"


def test_ai_coding_agent_result_dataclass():
    """Test AICodingAgentResult dataclass."""
    result = AICodingAgentResult(
        story_id="S1",
        blocked=True,
        blocked_reason="Test reason"
    )

    assert result.story_id == "S1"
    assert result.blocked == True
    assert result.blocked_reason == "Test reason"
    assert result.fix_attempts == 0  # default
    assert len(result.files_changed) == 0  # default
    assert len(result.test_suites) == 0  # default
    assert result.evidence_generated == False  # default


def test_successful_implementation_first_attempt():
    """Test successful implementation on first attempt with real file creation."""
    # Create a temporary directory to act as the repository
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            # Create a ready story
            story = Story(
                story_id="S1",
                title="Create a greeting function",
                user_story="As a user, I want a function that returns a greeting",
                acceptance_criteria=(
                    "The function must return the string 'Hello, World!'",
                ),
                tasks=(TechnicalTask(task_id="T1", title="Implement greeting function"),),
                component_ids=("comp1",),
                feature_ids=("feat1",)
            )

            # Mock AI client to return a successful implementation on first try
            mock_response = Mock()
            mock_response.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "greeting.py",
                        "content": '''def greet():
    return "Hello, World!"
'''
                    }
                ],
                "files_to_modify": [],
                "rationale": "Created greeting.py with the required function"
            })

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            # Create a test file that will test the greeting function
            with open("test_greeting.py", "w") as f:
                f.write('''
import greeting

def test_greet():
    assert greeting.greet() == "Hello, World!"
''')

            # Run the agent
            result = run_ai_coding_agent(story, client=mock_client)

            # Assertions
            assert result.blocked == False
            assert result.story_id == "S1"
            assert result.fix_attempts == 0
            assert result.evidence_generated == True
            assert len(result.files_changed) == 1
            # File path will be absolute
            assert result.files_changed[0].file_path.endswith("greeting.py")
            assert result.files_changed[0].change_type == "created"
            # Check that the file was actually created on disk
            assert os.path.exists("greeting.py")
            with open("greeting.py", "r") as f:
                content = f.read()
                assert 'def greet():' in content
                assert 'return "Hello, World!"' in content
            # Check that we have test suites (from the test run)
            assert len(result.test_suites) == 1
            assert result.test_suites[0].story_id == "S1"
            # The test should have passed
            assert len(result.test_suites[0].test_cases) == 1
            tc = result.test_suites[0].test_cases[0]
            assert tc.execution_status == TEST_EXECUTION_PASS
            assert "All tests passed" in tc.actual_result
        finally:
            os.chdir(original_cwd)


def test_fix_loop_one_retry_then_success():
    """Test that the agent retries once after a failure and then succeeds."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="Create a calculator",
                user_story="As a user, I want a function that adds two numbers",
                acceptance_criteria=(
                    "The function must return the sum of two numbers",
                ),
                tasks=(TechnicalTask(task_id="T1", title="Implement add function"),),
                component_ids=("comp1",),
                feature_ids=("feat1",)
            )

            # Mock AI client: first attempt returns buggy code, second attempt returns correct code
            mock_response_fail = Mock()
            mock_response_fail.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "calculator.py",
                        "content": '''def add(a, b):
    return a - b  # BUG: should be addition
'''
                    }
                ],
                "files_to_modify": [],
                "rationale": "Created calculator.py with subtraction bug"
            })

            mock_response_pass = Mock()
            mock_response_pass.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "calculator.py",
                        "content": '''def add(a, b):
    return a + b  # Fixed: correct addition
'''
                    }
                ],
                "files_to_modify": [],
                "rationale": "Fixed calculator.py to perform addition"
            })

            mock_client = Mock()
            mock_client.models.generate_content.side_effect = [
                mock_response_fail,  # first call
                mock_response_pass   # second call (after failure context added)
            ]

            # Create a simple test file that will fail on the first attempt and pass on the second
            with open("test_calculator.py", "w") as f:
                f.write('''
import calculator

def test_add():
    assert calculator.add(1, 2) == 3
''')

            # We'll mock subprocess.run to check if the file exists and run the test
            def mock_run_side_effect(*args, **kwargs):
                if "-m" in args[0] and "pytest" in args[0]:
                    # Check if calculator.py exists
                    if os.path.exists("calculator.py"):
                        # Read the file to check if it contains the correct add function
                        with open("calculator.py", "r") as f:
                            content = f.read()
                            # If it contains "return a + b", then the test should pass
                            if "return a + b" in content:
                                mock_result = Mock()
                                mock_result.returncode = 0
                                mock_result.stdout = "All tests passed"
                                mock_result.stderr = ""
                                return mock_result
                            else:
                                mock_result = Mock()
                                mock_result.returncode = 1
                                mock_result.stdout = ""
                                mock_result.stderr = "AssertionError: expected 3, got -1"
                                return mock_result
                    else:
                        # File doesn't exist yet
                        mock_result = Mock()
                        mock_result.returncode = 1
                        mock_result.stdout = ""
                        mock_result.stderr = "FileNotFoundError: No such file or directory: 'calculator.py'"
                        return mock_result
                # For other commands, return a default
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
                return mock_result

            with patch('coding_agent.subprocess.run', side_effect=mock_run_side_effect):
                result = run_ai_coding_agent(story, client=mock_client)

            # Assertions
            assert result.blocked == False
            assert result.story_id == "S1"
            assert result.fix_attempts == 1  # One fix attempt (second attempt succeeded)
            assert result.evidence_generated == True
            assert len(result.files_changed) == 1
            assert result.files_changed[0].file_path == "calculator.py"
            assert result.files_changed[0].change_type == "created"
            # Check that the file was created with correct content
            assert os.path.exists("calculator.py")
            with open("calculator.py", "r") as f:
                content = f.read()
                assert 'def add(a, b):' in content
                assert 'return a + b' in content
                assert 'return a - b' not in content  # Bug fixed
            # Check test results show success on the final attempt
            assert len(result.test_suites) == 1
            assert len(result.test_suites[0].test_cases) == 1
            tc = result.test_suites[0].test_cases[0]
            assert tc.execution_status == TEST_EXECUTION_PASS
            assert "All tests passed" in tc.actual_result
        finally:
            os.chdir(original_cwd)


def test_blocked_after_max_attempts():
    """Test that the agent becomes blocked after 3 failed attempts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="Implement impossible feature",
                user_story="As a user, I want a function that always returns True and False at the same time",
                acceptance_criteria=(
                    "The function must return True and False simultaneously",
                ),
                tasks=(TechnicalTask(task_id="T1", title="Implement paradox function"),),
                component_ids=("comp1",),
                feature_ids=("feat1",)
            )

            # Mock AI client to always return code that fails the test
            mock_response = Mock()
            mock_response.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "paradox.py",
                        "content": '''def paradox():
    return True  # This will fail the test that expects both True and False
'''
                    }
                ],
                "files_to_modify": [],
                "rationale": "Created paradox.py that only returns True"
            })

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            # We'll mock subprocess.run to always return a failure
            def mock_run_side_effect(*args, **kwargs):
                if args[0][1] == "-m" and args[0][2] == "pytest":
                    # This is the pytest call
                    mock_result = Mock()
                    mock_result.returncode = 1
                    mock_result.stdout = ""
                    mock_result.stderr = "AssertionError: Function must return both True and False"
                    return mock_result
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
                return mock_result

            with patch('coding_agent.subprocess.run', side_effect=mock_run_side_effect):
                result = run_ai_coding_agent(story, client=mock_client)

            # Assertions
            assert result.blocked == True
            assert result.story_id == "S1"
            assert result.fix_attempts == 3  # Max attempts reached (0,1,2,3 -> 3 attempts after first)
            assert "Failed to pass tests after 3 corrective attempts" in result.blocked_reason
            assert result.evidence_generated == True  # Evidence generated from the attempts
            assert len(result.files_changed) == 1
            assert result.files_changed[0].file_path.endswith("paradox.py")
            assert result.files_changed[0].change_type == "created"
            # Check the file was created
            assert os.path.exists("paradox.py")
            # Check that we have test suites from the final attempt
            assert len(result.test_suites) == 1
            assert len(result.test_suites[0].test_cases) == 1
            tc = result.test_suites[0].test_cases[0]
            assert tc.execution_status == TEST_EXECUTION_FAIL
            assert "Function must return both True and False" in tc.actual_result
        finally:
            os.chdir(original_cwd)


def test_security_path_validation():
    """Test that the agent rejects paths outside the repository root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="Path traversal test",
                user_story="As a user, I want to test path security",
                acceptance_criteria=("Should not allow writing outside repo",),
                tasks=(TechnicalTask(task_id="T1", title="Test security"),),
                component_ids=("comp1",),
                feature_ids=("feat1",)
            )

            # Mock AI client to attempt to write outside the repo
            mock_response = Mock()
            mock_response.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "../../outside_repo.txt",  # Path traversal
                        "content": "should not be written"
                    }
                ],
                "files_to_modify": [],
                "rationale": "Attempted path traversal"
            })

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            result = run_ai_coding_agent(story, client=mock_client)

            # Should be blocked due to security error
            assert result.blocked == True
            assert "Path escapes workspace root" in result.blocked_reason
            assert result.story_id == "S1"
            assert result.fix_attempts == 0
            assert result.evidence_generated == False
            # Ensure no file was actually created outside the repo
            assert not os.path.exists(os.path.join(tmpdir, "..", "..", "outside_repo.txt"))
        finally:
            os.chdir(original_cwd)