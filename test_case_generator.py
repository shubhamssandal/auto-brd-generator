import json
import logging
from typing import Optional

from google import genai
from google.genai import types

from implementation_plan_models import Story, ImplementationPlan
from test_case_models import TestCase, TestSuite

logger = logging.getLogger(__name__)


def _generate_test_cases_for_story(story: Story, client: Optional[genai.Client] = None) -> list[TestCase]:
    """Generate test cases for a single story."""
    if client is None:
        client = genai.Client()

    prompt = (
        f"You are a QA engineer. Your task is to generate comprehensive test cases for the following user story:\n\n"
        f"Story ID: {story.story_id}\n"
        f"Title: {story.title}\n"
        f"User Story: {story.user_story}\n"
        f"Acceptance Criteria:\n" + "\n".join(f"- {criterion}" for criterion in story.acceptance_criteria) + "\n\n"
        f"Generate test cases covering Functional, Negative, Edge Case, Integration, and Security tests.\n"
        f"Each test case should include: Test ID, Scenario, Preconditions, Steps, Expected Result, Priority, and Test Type.\n"
        f"Return the test cases as a JSON array with the following schema:\n"
        f"[\n"
        f"  {{\n"
        f"    \"test_id\": \"string\",\n"
        f"    \"story_reference\": \"{story.story_id}\",\n"
        f"    \"scenario\": \"string\",\n"
        f"    \"preconditions\": \"string\",\n"
        f"    \"steps\": \"string\",\n"
        f"    \"expected_result\": \"string\",\n"
        f"    \"priority\": \"High\" | \"Medium\" | \"Low\",\n"
        f"    \"test_type\": \"Functional\" | \"Negative\" | \"Edge Case\" | \"Integration\" | \"Security\"\n"
        f"  }}\n"
        f"]"
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        data = json.loads(response.text)
        return [TestCase(**test_case) for test_case in data]
    except Exception as e:
        logger.warning("Test case generation failed for story %s: %s", story.story_id, e)
        return []


def generate_test_suite(plan: ImplementationPlan, client: Optional[genai.Client] = None) -> list[TestSuite]:
    """Generate a complete test suite from an implementation plan."""
    test_suites = []
    for story in plan.stories:
        test_cases = _generate_test_cases_for_story(story, client)
        test_suites.append(TestSuite(story_id=story.story_id, test_cases=test_cases))
    return test_suites


def _fallback_test_suite(plan: ImplementationPlan) -> list[TestSuite]:
    """Generate a fallback test suite when AI client is unavailable."""
    test_suites = []
    for story in plan.stories:
        test_cases = [
            TestCase(
                test_id=f"{story.story_id}-F1",
                story_reference=story.story_id,
                scenario=f"Verify {story.title} functionality",
                preconditions=f"System is in a valid state for {story.title}",
                steps=f"1. Perform the action described in {story.title}\n2. Verify the expected outcome",
                expected_result=f"The system should {story.user_story.split('so that')[1].strip() if 'so that' in story.user_story else 'complete the action successfully'}",
                priority="Medium",
                test_type="Functional"
            )
        ]
        test_suites.append(TestSuite(story_id=story.story_id, test_cases=test_cases))
    return test_suites