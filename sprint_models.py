from dataclasses import dataclass, field
from typing import Optional

@dataclass
class SprintIssue:
    issue_key: str = ""
    summary: str = ""
    rationale: str = ""
    is_selected: bool = False
    story_id: str = ""

@dataclass
class SprintPlan:
    sprint_name: str = "Sprint 1"
    sprint_goal: str = ""
    duration_weeks: int = 4
    issues: list = field(default_factory=list)
    approved: bool = False  # New flag to enforce sprint approval before execution

    @property
    def selected_issues(self):
        return [i for i in self.issues if i.is_selected]

    @property
    def deferred_issues(self):
        return [i for i in self.issues if not i.is_selected]
