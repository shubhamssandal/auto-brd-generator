# Auto-BRD Generator — Frontend Specification

## Existing Main Flow
Transcript source selection:
- Manual Paste
- Upload Transcript File (.txt)
- Google Meet
- Microsoft Teams

Then:
Retrieve/Provide Transcript
→ Process Transcript
→ Generate BRD
→ Review BRD
→ Jira (optional)

## Jira Entry Point
After a valid BRD is available:

Disconnected:
[ Connect Jira ]

Connected:
Connected to Jira
[ Disconnect ]

Jira must remain optional for BRD-only users.

## Site and Project Selection
If multiple sites exist:
Select Jira Site
[ Dropdown ]

Then:
Select Jira Project
[ Dropdown ]

Show loading states during API calls.

## Generate Work Plan
[ Generate Jira Work Plan ]

This generates a proposal only.

Hierarchy:
Epic
  ├── Story / Task
  │     ├── Description
  │     ├── Acceptance Criteria
  │     └── Subtasks
  └── Story / Task

## Review Interface
Support:
- Title editing
- Description editing
- Acceptance-criteria editing
- Issue-type selection where supported
- Selection checkbox
- Delete action

Parent/child relationships must be visually clear.

## Validation
Block creation for:
- No project selected
- Missing required field
- Unsupported issue type
- Invalid parent/child selection

## Confirmation
Primary action:
[ Create Selected Issues in Jira ]

Before execution, summarize number of items, target project, and issue types.

## Success
Show:
Success: X issues created

For each created item:
- Issue key
- Summary
- Jira link

For partial failure, clearly separate created and failed items.

## Errors
Provide actionable messages for OAuth failure, permission denial, no accessible sites/projects, missing fields, and creation failures without exposing sensitive internals.

## Design Principles
- Keep the Streamlit UI coherent
- Use progressive disclosure
- Keep write actions visually distinct
- Preserve existing Google Meet and Teams workflows
