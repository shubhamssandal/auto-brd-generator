# Auto-BRD Generator — Feature Ticket List

## Development Rules
- Implement one ticket at a time.
- Do not make unrelated changes.
- Run relevant tests after each ticket.
- Commit after each verified milestone.
- Do not begin the next ticket without confirmation.
- Do not perform broad autonomous refactoring or investigations.

---

## JIRA-001 — Jira Integration Foundation
Goal: Add minimum configuration and module structure.

Acceptance Criteria:
- Jira configuration is isolated
- Secrets use environment variables
- Existing functionality remains unchanged
- No real Jira write action yet

---

## JIRA-002 — Jira OAuth Connection
Goal: Connect a Jira Cloud account.

Acceptance Criteria:
- Start authorization
- Validate callback
- Secure token handling
- Connected/disconnected UI state
- Tokens never displayed

---

## JIRA-003 — Accessible Site Discovery
Goal: Retrieve accessible Jira Cloud sites.

Acceptance Criteria:
- Dynamic retrieval
- Multiple sites supported
- User can select a site
- Empty access handled clearly

---

## JIRA-004 — Project and Metadata Discovery
Goal: Retrieve projects and project-specific metadata.

Acceptance Criteria:
- Dynamic projects
- Project selection
- Relevant issue types discovered
- Required fields validated
- No hardcoded hierarchy

---

## JIRA-005 — BRD to Jira Work Plan
Goal: Convert selected BRD requirements into a reviewable plan.

Acceptance Criteria:
- Generates proposed Epics/Stories/Tasks/Subtasks as supported
- Includes descriptions and acceptance criteria
- Creates no Jira issues
- Uses validated models

---

## JIRA-006 — Jira Work Plan Review
Goal: Allow editing before creation.

Acceptance Criteria:
- Edit fields
- Delete items
- Select/deselect items
- Preserve hierarchy
- Validate relationships

---

## JIRA-007 — Create Approved Jira Issues
Goal: Create explicitly selected and validated items.

Acceptance Criteria:
- Explicit confirmation
- Parent before dependent children
- Return issue keys
- Clear partial failures
- No hidden duplicate retries

---

## JIRA-008 — Result Mapping and Traceability
Goal: Show relationship between requirements/work plan and created issues.

Acceptance Criteria:
- Issue keys and links
- Creation status
- Mapping available during workflow

---

## JIRA-009 — Regression and Integration Testing
Acceptance Criteria:
- Manual Paste works
- Transcript upload works
- Google Meet works
- Microsoft Teams works
- BRD generation works
- Existing and new tests pass

---

## Future: JIRA-010 — Requirement Change Detection
Not part of the first Jira milestone.

Goal: Compare new meeting decisions with existing BRD/Jira-linked requirements and propose changes.

Rule: No BRD or Jira change occurs automatically; user approval is required.
