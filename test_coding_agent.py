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


def test_repository_context_included_in_prompt():
    """Test that repository context is included in the AI prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            # Create a ready story
            story = Story(
                story_id="S1",
                title="Test Story",
                user_story="As a user, I want to do something",
                acceptance_criteria=("Criterion 1",),
                tasks=(TechnicalTask(task_id="T1", title="Implement something"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Create some source files to appear in repository context
            with open("main.py", "w") as f:
                f.write("# Main application\n")
            with open("utils.py", "w") as f:
                f.write("# Utility functions\n")
            os.makedirs("tests", exist_ok=True)
            with open("tests/test_main.py", "w") as f:
                f.write("def test_main():\n    pass\n")

            # Mock AI client to capture the prompt
            captured_prompts = []

            def capture_prompt(*args, **kwargs):
                # Extract the prompt from the call
                if 'contents' in kwargs:
                    captured_prompts.append(kwargs['contents'])
                elif args:
                    captured_prompts.append(args[0] if isinstance(args[0], str) else str(args[0]))

                # Return a simple valid JSON response
                mock_response = Mock()
                mock_response.text = json.dumps({
                    "files_to_create": [
                        {
                            "path": "test.py",
                            "content": "# Test file\nprint('hello')\n"
                        }
                    ],
                    "files_to_modify": [],
                    "rationale": "Created test file"
                })
                return mock_response

            mock_client = Mock()
            mock_client.models.generate_content.side_effect = capture_prompt

            # Mock subprocess.run to return success (so the agent doesn't get blocked on test failures)
            def mock_run_success(*args, **kwargs):
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "All tests passed"
                mock_result.stderr = ""
                return mock_result

            with patch('coding_agent.subprocess.run', side_effect=mock_run_success):
                # Run the agent
                result = run_ai_coding_agent(story, client=mock_client)

                # Verify the agent succeeded
                assert result.blocked == False
                assert result.story_id == "S1"

                # Verify that prompts were captured
                assert len(captured_prompts) > 0

                # Check that the prompt contains repository context elements
                # The prompt should contain the story details
                prompt_content = " ".join(captured_prompts)
                assert "S1" in prompt_content
                assert "Test Story" in prompt_content
                assert "As a user, I want to do something" in prompt_content
                assert "Criterion 1" in prompt_content
                # Check for repository context indicators
                assert "Repository tree" in prompt_content or "main.py" in prompt_content or "utils.py" in prompt_content

        finally:
            os.chdir(original_cwd)


def test_actual_file_creation_and_modification():
    """Test that actual file creation and modification happens in tmp workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="File Operations Test",
                user_story="As a user, I want to test file operations",
                acceptance_criteria=("Create and modify files correctly",),
                tasks=(TechnicalTask(task_id="T1", title="Create and modify files"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Mock AI client to return file creation and modification
            mock_response = Mock()
            mock_response.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "new_file.py",
                        "content": "# New file\nprint('hello')\n"
                    }
                ],
                "files_to_modify": [
                    {
                        "path": "existing_file.py",
                        "new_content": "# Modified file\nprint('world')\n"
                    }
                ],
                "rationale": "Created and modified files"
            })

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            # Create an existing file to modify
            with open("existing_file.py", "w") as f:
                f.write("# Original file\nprint('original')\n")

            # Mock subprocess.run to return success (so the agent doesn't get blocked on test failures)
            def mock_run_success(*args, **kwargs):
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "All tests passed"
                mock_result.stderr = ""
                return mock_result

            with patch('coding_agent.subprocess.run', side_effect=mock_run_success):
                # Run the agent
                result = run_ai_coding_agent(story, client=mock_client)

                # Verify the agent succeeded
                assert result.blocked == False
                assert result.story_id == "S1"

                # Verify files were actually created and modified
                assert len(result.files_changed) == 2

                # Check new file was created
                new_file_change = next((c for c in result.files_changed if c.file_path.endswith("new_file.py")), None)
                assert new_file_change is not None
                assert new_file_change.change_type == "created"
                assert os.path.exists("new_file.py")
                with open("new_file.py", "r") as f:
                    assert f.read() == "# New file\nprint('hello')\n"

                # Check existing file was modified
                modified_file_change = next((c for c in result.files_changed if c.file_path.endswith("existing_file.py")), None)
                assert modified_file_change is not None
                assert modified_file_change.change_type == "modified"
                assert os.path.exists("existing_file.py")
                with open("existing_file.py", "r") as f:
                    assert f.read() == "# Modified file\nprint('world')\n"

        finally:
            os.chdir(original_cwd)


def test_protected_paths_cannot_be_written():
    """Test that protected paths such as .git, .env*, credentials, secrets, and private keys cannot be written."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="Protected Paths Test",
                user_story="As a user, I want to test path security",
                acceptance_criteria=("Should not allow writing to protected paths",),
                tasks=(TechnicalTask(task_id="T1", title="Test security"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Test .git directory
            mock_response_git = Mock()
            mock_response_git.text = json.dumps({
                "files_to_create": [
                    {
                        "path": ".git/config",
                        "content": "[core]\n"
                    }
                ],
                "files_to_modify": [],
                "rationale": "Attempted to write to .git"
            })

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response_git

            result = run_ai_coding_agent(story, client=mock_client)
            assert result.blocked == True
            assert "Path escapes workspace root" in result.blocked_reason or "Absolute paths are not allowed" in result.blocked_reason
            assert result.story_id == "S1"

            # Test secrets directory
            mock_response_secrets = Mock()
            mock_response_secrets.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "secrets/private.key",
                        "content": "-----BEGIN PRIVATE KEY-----\n"
                    }
                ],
                "files_to_modify": [],
                "rationale": "Attempted to write to secrets"
            })

            mock_client.models.generate_content.return_value = mock_response_secrets
            result = run_ai_coding_agent(story, client=mock_client)
            assert result.blocked == True
            assert "Path escapes workspace root" in result.blocked_reason or "Absolute paths are not allowed" in result.blocked_reason
            assert result.story_id == "S1"

            # Test that no protected files were actually created
            assert not os.path.exists(".git")
            assert not os.path.exists("secrets")

        finally:
            os.chdir(original_cwd)


def test_malformed_ai_response_handled_safely():
    """Test that malformed AI responses are handled safely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="Malformed Response Test",
                user_story="As a user, I want to test error handling",
                acceptance_criteria=("Handle malformed responses",),
                tasks=(TechnicalTask(task_id="T1", title="Test error handling"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Mock AI client to return malformed JSON
            mock_response = Mock()
            mock_response.text = "This is not valid JSON { invalid json"

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            # Run the agent
            result = run_ai_coding_agent(story, client=mock_client)

            # Should be blocked due to unparseable response
            assert result.blocked == True
            assert result.story_id == "S1"
            assert "AI client response could not be parsed as JSON" in result.blocked_reason
            assert result.fix_attempts == 0
            assert result.evidence_generated == False

        finally:
            os.chdir(original_cwd)


def test_ai_failure_handled_safely():
    """Test that AI call failures are handled safely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="AI Failure Test",
                user_story="As a user, I want to test AI failure handling",
                acceptance_criteria=("Handle AI failures",),
                tasks=(TechnicalTask(task_id="T1", title="Test AI failure"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Mock AI client to raise an exception
            mock_client = Mock()
            mock_client.models.generate_content.side_effect = Exception("API call failed")

            # Run the agent
            result = run_ai_coding_agent(story, client=mock_client)

            # Should be blocked due to AI client unavailability
            assert result.blocked == True
            assert result.story_id == "S1"
            assert "AI client unavailable or returned no response" in result.blocked_reason
            assert result.fix_attempts == 0
            assert result.evidence_generated == False

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


def test_repository_context_reaches_model_prompt():
    """Test that repository context is included in the AI prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            # Create a ready story
            story = Story(
                story_id="S1",
                title="Test Story",
                user_story="As a user, I want to do something",
                acceptance_criteria=("Criterion 1",),
                tasks=(TechnicalTask(task_id="T1", title="Implement something"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Mock AI client to capture the prompt
            captured_prompts = []

            def capture_prompt(*args, **kwargs):
                # Extract the prompt from the call
                if 'contents' in kwargs:
                    captured_prompts.append(kwargs['contents'])
                elif args:
                    captured_prompts.append(args[0] if isinstance(args[0], str) else str(args[0]))

                # Return a simple valid JSON response
                mock_response = Mock()
                mock_response.text = json.dumps({
                    "files_to_create": [
                        {
                            "path": "test.py",
                            "content": "# Test file\nprint('hello')\n"
                        }
                    ],
                    "files_to_modify": [],
                    "rationale": "Created test file"
                })
                return mock_response

            mock_client = Mock()
            mock_client.models.generate_content.side_effect = capture_prompt

            # Mock subprocess.run to return success (so the agent doesn't get blocked on test failures)
            def mock_run_success(*args, **kwargs):
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "All tests passed"
                mock_result.stderr = ""
                return mock_result

            with patch('coding_agent.subprocess.run', side_effect=mock_run_success):
                # Run the agent
                result = run_ai_coding_agent(story, client=mock_client)

                # Verify the agent succeeded
                assert result.blocked == False
                assert result.story_id == "S1"

                # Verify that prompts were captured
                assert len(captured_prompts) > 0

                # Check that the prompt contains repository context elements
                # The prompt should contain the story details
                prompt_content = " ".join(captured_prompts)
                assert "S1" in prompt_content
                assert "Test Story" in prompt_content
                assert "As a user, I want to do something" in prompt_content
                assert "Criterion 1" in prompt_content

        finally:
            os.chdir(original_cwd)


def test_actual_create_modify_in_tmp_workspace():
    """Test that actual file creation and modification happens in tmp workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="File Operations Test",
                user_story="As a user, I want to test file operations",
                acceptance_criteria=("Create and modify files correctly",),
                tasks=(TechnicalTask(task_id="T1", title="Create and modify files"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Mock AI client to return file creation and modification
            mock_response = Mock()
            mock_response.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "new_file.py",
                        "content": "# New file\nprint('hello')\n"
                    }
                ],
                "files_to_modify": [
                    {
                        "path": "existing_file.py",
                        "new_content": "# Modified file\nprint('world')\n"
                    }
                ],
                "rationale": "Created and modified files"
            })

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            # Create an existing file to modify
            with open("existing_file.py", "w") as f:
                f.write("# Original file\nprint('original')\n")

            # Mock subprocess.run to return success (so the agent doesn't get blocked on test failures)
            def mock_run_success(*args, **kwargs):
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "All tests passed"
                mock_result.stderr = ""
                return mock_result

            with patch('coding_agent.subprocess.run', side_effect=mock_run_success):
                # Run the agent
                result = run_ai_coding_agent(story, client=mock_client)

                # Verify the agent succeeded
                assert result.blocked == False
                assert result.story_id == "S1"

                # Verify files were actually created and modified
                assert len(result.files_changed) == 2

                # Check new file was created
                new_file_change = next((c for c in result.files_changed if c.file_path.endswith("new_file.py")), None)
                assert new_file_change is not None
                assert new_file_change.change_type == "created"
                assert os.path.exists("new_file.py")
                with open("new_file.py", "r") as f:
                    assert f.read() == "# New file\nprint('hello')\n"

                # Check existing file was modified
                modified_file_change = next((c for c in result.files_changed if c.file_path.endswith("existing_file.py")), None)
                assert modified_file_change is not None
                assert modified_file_change.change_type == "modified"
                assert os.path.exists("existing_file.py")
                with open("existing_file.py", "r") as f:
                    assert f.read() == "# Modified file\nprint('world')\n"

        finally:
            os.chdir(original_cwd)


def test_malformed_model_response_blocks_safely():
    """Test that malformed model responses are handled safely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="Malformed Response Test",
                user_story="As a user, I want to test error handling",
                acceptance_criteria=("Handle malformed responses",),
                tasks=(TechnicalTask(task_id="T1", title="Test error handling"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Mock AI client to return malformed JSON
            mock_response = Mock()
            mock_response.text = "This is not valid JSON { invalid json"

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            # Run the agent
            result = run_ai_coding_agent(story, client=mock_client)

            # Should be blocked due to unparseable response
            assert result.blocked == True
            assert result.story_id == "S1"
            assert "AI client response could not be parsed as JSON" in result.blocked_reason
            assert result.fix_attempts == 0
            assert result.evidence_generated == False

        finally:
            os.chdir(original_cwd)


def test_model_call_failure_blocks_safely():
    """Test that model call failures are handled safely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="S1",
                title="Model Failure Test",
                user_story="As a user, I want to test model failure handling",
                acceptance_criteria=("Handle model failures",),
                tasks=(TechnicalTask(task_id="T1", title="Test model failure"),),
                component_ids=("comp1",),
                feature_ids=("feat1")
            )

            # Mock AI client to raise an exception
            mock_client = Mock()
            mock_client.models.generate_content.side_effect = Exception("API call failed")

            # Run the agent
            result = run_ai_coding_agent(story, client=mock_client)

            # Should be blocked due to AI client unavailability
            assert result.blocked == True
            assert result.story_id == "S1"
            assert "AI client unavailable or returned no response" in result.blocked_reason
            assert result.fix_attempts == 0
            assert result.evidence_generated == False

        finally:
            os.chdir(original_cwd)


def test_result_uses_correct_story():
    """Test that the result correctly references the input story."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            story = Story(
                story_id="USER-123",
                title="User Story 123",
                user_story="As a user, I want feature XYZ",
                acceptance_criteria=("Criterion A", "Criterion B"),
                tasks=(TechnicalTask(task_id="T1", title="Implement feature"),),
                component_ids=("comp1", "comp2"),
                feature_ids=("feat1",)
            )

            # Mock AI client to return a simple response
            mock_response = Mock()
            mock_response.text = json.dumps({
                "files_to_create": [
                    {
                        "path": "feature.py",
                        "content": "# Feature implementation\n"
                    }
                ],
                "files_to_modify": [],
                "rationale": "Implemented feature"
            })

            mock_client = Mock()
            mock_client.models.generate_content.return_value = mock_response

            # Mock subprocess.run to return success (so the agent doesn't get blocked on test failures)
            def mock_run_success(*args, **kwargs):
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "All tests passed"
                mock_result.stderr = ""
                return mock_result

            with patch('coding_agent.subprocess.run', side_effect=mock_run_success):
                # Run the agent
                result = run_ai_coding_agent(story, client=mock_client)

                # Verify the result uses the correct story ID
                assert result.story_id == "USER-123"
                assert result.blocked == False

                # Verify test suites reference the correct story
                assert len(result.test_suites) > 0
                for suite in result.test_suites:
                    assert suite.story_id == "USER-123"
                    for test_case in suite.test_cases:
                        assert test_case.story_reference == "USER-123"

        finally:
            os.chdir(original_cwd)
