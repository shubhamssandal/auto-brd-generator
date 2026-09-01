from dataclasses import dataclass, field
from typing import Optional

# Execution statuses for test execution stage
TEST_EXECUTION_NOT_RUN = "Not Run"
TEST_EXECUTION_PASS = "Pass"
TEST_EXECUTION_FAIL = "Fail"
TEST_EXECUTION_BLOCKED = "Blocked"

# Execution statuses for test execution stage
TEST_EXECUTION_NOT_RUN = "Not Run"
TEST_EXECUTION_PASS = "Pass"
TEST_EXECUTION_FAIL = "Fail"
TEST_EXECUTION_BLOCKED = "Blocked"

@dataclass
class TestCase:
    test_id: str
    story_reference: str
    scenario: str
    preconditions: str = ""
    steps: str = ""
    expected_result: str = ""
    priority: str = "Medium"
    test_type: str = "Functional"
    is_approved: bool = False

    # Test execution tracking
    execution_status: str = TEST_EXECUTION_NOT_RUN
    actual_result: str = ""
    notes: str = ""
    defect_reference: str = ""

@dataclass
class TestSuite:
    story_id: str
    test_cases: list[TestCase] = field(default_factory=list)

    @property
    def approved_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.is_approved]

    @property
    def is_empty(self) -> bool:
        return not self.test_cases

    @property
    def functional_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.test_type == "Functional"]

    @property
    def negative_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.test_type == "Negative"]

    @property
    def edge_case_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.test_type == "Edge Case"]

    @property
    def integration_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.test_type == "Integration"]

    @property
    def security_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.test_type == "Security"]

    @property
    def high_priority_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.priority == "High"]

    @property
    def medium_priority_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.priority == "Medium"]

    @property
    def low_priority_tests(self) -> list[TestCase]:
        return [t for t in self.test_cases if t.priority == "Low"]

    @property
    def test_types(self) -> set:
        return {t.test_type for t in self.test_cases}

    @property
    def priorities(self) -> set:
        return {t.priority for t in self.test_cases}