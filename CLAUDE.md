# Auto-BRD → AI Software Delivery Lifecycle

## 1. Product Vision

This project is evolving from a BRD/Jira automation tool into an **AI-assisted software delivery lifecycle platform**.

The goal is to take a software product idea from stakeholder discovery through:

Discovery → BRD → PRD → Architecture → Jira Implementation Plan → Sprint Planning → Test Cases → Test Execution / Delivery Tracking.

The platform is **domain-agnostic**. The domain arrives entirely through the uploaded transcript and the artifacts derived from it, so the same engine must handle e-commerce, social, streaming, fintech, SaaS and enterprise products without domain-specific code. E-commerce is the primary demonstration domain, not a hard-coded limitation: no module should branch on the domain.

The system should help a System Analyst / Business Analyst / Product Manager progressively transform business discussions into structured software-delivery artifacts.

The system must remain human-in-the-loop. AI should propose artifacts and plans; users should review and explicitly approve important outputs before downstream stages are committed.

### Implementation status

This document is the **target specification**. Stages it describes with "should" are requirements, not claims about what exists. What is built today:

| Stage | Status |
| ----- | ------ |
| Discovery → BRD | Implemented (four ingestion sources, evidence validation, Markdown export, explicit approval) |
| BRD → PRD | Implemented (`prd_generator.py`, review/edit, explicit approval) |
| PRD → Architecture | Implemented (`architecture_generator.py`, review/edit, explicit approval) |
| Architecture → Implementation Plan | Implemented (`implementation_plan_generator.py`, epic/story/task hierarchy, review/edit, explicit approval; writes nothing to Jira) |
| Implementation Plan → Jira | Implemented (`implementation_plan_jira.py`, mapping onto the selected project's own issue types, preview, explicit confirmation, idempotent creation, stable plan id → issue key mapping) |
| Sprint Planning | **Not implemented.** |
| Story → Test Cases | **Not implemented.** |
| Test Execution / Delivery Tracking | Partially implemented: Jira creation results and the plan → issue delivery mapping are recorded and displayed. No issue is read back from Jira, so no workflow status is polled, and test-case execution recording is not built. |
| AI Coding Agent | **Not implemented.** |
| Sprint Completion / Next Sprint | **Not implemented.** |

Keep this table accurate. A stage is "Implemented" only when it has a generator, a human approval gate and tests.

---

## 2. Source-of-Truth Model

Use clear ownership:

- **BRD** = business requirements source of truth
- **PRD** = product behaviour / detailed product definition
- **Architecture** = technical design source of truth
- **Jira** = software delivery / work tracking source of truth
- **Git/GitHub** = source-code source of truth
- **Test results** = quality / verification evidence

Do NOT create automatic Jira → BRD synchronization.

A Jira workflow status change such as:

To Do → In Progress → Done

is an implementation-status change, NOT a requirement change.

The application may read and display Jira execution status, but must not modify the BRD because of a Jira status change.

---

## 3. Target End-to-End Workflow

### Stage 1 — Discovery → BRD

Input:
- Discovery meeting transcript
- Manual paste or uploaded transcript
- Existing supported transcript sources may be reused

Output:
- Structured BRD

BRD should cover:
- Business problem
- Business objectives
- Stakeholders
- Scope
- Functional requirements
- Non-functional requirements
- Business rules
- Assumptions
- Dependencies
- Risks
- Open questions

User reviews/approves BRD before continuing.

---

### Stage 2 — BRD → PRD

The PRD stage does NOT require a second transcript.

Primary input:
- Approved BRD

Optional input:
- Product refinement / product discussion transcript

The system should be able to generate a PRD directly from the approved BRD, while allowing an optional product discussion transcript to enrich or refine it.

PRD should add product-level detail such as:
- Features
- User journeys
- Detailed functional behaviour
- Edge cases
- User stories where appropriate
- Acceptance criteria
- Product assumptions
- Success metrics where supported

Do not simply duplicate the BRD.

Conceptual relationship:

Approved BRD
+
Optional Product Refinement Discussion
→ PRD

---

### Stage 3 — PRD → Architecture

Generate a proposed technical architecture for:

- Backend
- Web application
- Mobile application

Architecture should cover, where supported:
- Major components/services
- API boundaries
- Data/storage approach
- Authentication/authorization
- External integrations
- Main data flows
- Important architecture decisions
- Dependencies
- Technical risks

The architecture should be derived from the approved PRD.

A separate architecture transcript may be optionally provided as additional evidence.

---

### Stage 4 — Architecture → Implementation Plan

Generate an implementation plan from the approved architecture + PRD.

Break work into:

Epic
→ Stories
→ Technical Tasks / Subtasks

Do not force every piece of technical work into a user story.

Use appropriate work types:
- Epic
- Story
- Task
- Subtask

Example:

Epic:
Checkout & Ordering

Story:
As a customer, I want to complete checkout so that I can purchase products.

Technical tasks/subtasks may include:
- Database model
- Backend API
- Inventory validation
- Payment integration
- Web UI
- Mobile UI
- Automated tests

Each story should ideally contain:
- User story / description
- Acceptance criteria
- Dependencies
- Priority
- Relevant architecture component
- Technical implementation work

---

## 5. Jira Integration

Reuse the existing Jira OAuth/integration where appropriate.

Jira should represent the delivery plan and execution state.

The platform may:
- Create Epics
- Create Stories
- Create Tasks/Subtasks where supported
- Link work to requirements / architecture / PRD context
- Create and manage sprint scope
- Read current Jira status
- Display execution progress

The platform must NOT:
- Treat Jira as the BRD source of truth
- Automatically rewrite the BRD because Jira content/status changed
- Implement bidirectional requirement synchronization

---

## 6. Sprint Planning

After Epics/Stories/Tasks are created, the platform should provide Sprint Planning.

Default planning concept:
- Sprint duration: 1 month unless the user changes it

AI should propose a Sprint 1 based on:
- Business priority
- Dependencies
- Architecture dependencies
- Estimated complexity/effort if available
- Technical prerequisites
- Story readiness

The AI recommendation must be reviewable and editable.

Example:

Sprint 1 Goal:
Customer purchase foundation

Selected:
- Cart management
- Checkout foundation
- Required backend tasks
- Required web/mobile tasks
- Required tests

User explicitly approves the sprint scope.

---

## 7. Story → Test Cases

For selected sprint stories, generate test cases from:
- Story
- Acceptance criteria
- Relevant PRD context
- Relevant architecture context

Test cases can include:
- Functional
- Negative
- Edge case
- Integration
- Security

Each test case should contain, where practical:
- Test ID
- Story reference
- Scenario
- Preconditions
- Steps
- Expected result
- Priority
- Test type

Security testing may consider areas such as:
- Authentication
- Signup/verification
- Session security
- Error-message leakage
- Password/security controls
- MFA where applicable
- Backend/API security
- Logging/monitoring
- Authorization/RBAC

Do not force irrelevant security tests onto every story.

---

## 8. Test Execution / Delivery Tracking

Initial implementation should support recording execution results rather than pretending the application can execute arbitrary production builds automatically.

Supported result states may include:
- Not Run
- Pass
- Fail
- Blocked

Also capture:
- Actual result
- Notes
- Defect/bug reference where applicable

The intended lifecycle is:

Story
→ Technical Tasks
→ Implementation
→ Test Cases
→ Build available
→ Test Execution
→ Evidence
→ Jira progress / completion

Do NOT mark Jira issues Done merely because AI generated code.

Completion should be based on appropriate evidence available in the system, such as:
- Implementation completed
- Required review completed
- Relevant tests passed
- User confirmation where applicable

Automatic code generation / automated deployment is NOT an immediate scope requirement.

---

## 9. Sprint-to-Sprint Lifecycle

The system should support multiple sprints.

Example:

Sprint 1
→ plan
→ execute
→ test
→ review
→ complete

Then:

Sprint 2
→ recalculate remaining work
→ recommend next scope
→ user approves
→ execute
→ test
→ complete

The system should preserve completed sprint history.

---

## 10. Reuse Existing Project Capabilities

Preserve useful existing capabilities where they fit the new product:

- Transcript ingestion
- Manual transcript entry
- TXT upload
- Google Meet support
- Microsoft Teams support
- BRD generation
- BRD validation/review
- Jira OAuth
- Jira issue creation
- Existing Jira hierarchy/work-plan support
- Existing traceability mechanisms
- Existing test infrastructure

Do not throw away working foundations unnecessarily.

---

## 11. Remove Obsolete Functionality

The previous JIRA-010 requirement-change-governance workflow is no longer part of the target product.

Remove obsolete functionality associated specifically with:
- Meeting-driven requirement change proposals
- Jira-driven requirement drift detection
- Accept Jira → BRD
- Keep Jira only requirement governance
- Jira → BRD synchronization
- Requirement-change approval/rejection workflows used only by that feature
- Immutable Jira requirement baselines used only for drift detection

Before deleting anything, identify shared infrastructure that is still needed by the new workflow.

Do not delete useful Jira execution/status functionality.

---

## 12. UI Direction

The application should evolve from a long single-page generator into a clear project workspace.

Suggested navigation:

1. Discovery → BRD
2. Product Definition → PRD
3. Architecture
4. Implementation Plan
5. Sprint Planning
6. Test Cases
7. Test Execution
8. Jira / Delivery Status

Each stage should have an understandable state such as:
- Not Started
- Draft
- Pending Review
- Approved
- In Progress
- Completed

Avoid unnecessary UI complexity.

---

## 13. AI + Human Governance

AI should:
- Generate
- Recommend
- Decompose
- Prioritize
- Explain

Human should:
- Review
- Edit
- Approve
- Reject
- Confirm important transitions

Do not silently move an artifact into an approved state.

---

## 14. Implementation Principles

- Current repository is the source of truth.
- Inspect before modifying.
- Reuse working architecture where practical.
- Prefer small, testable modules.
- Avoid large unrelated refactors.
- Avoid speculative features.
- Do not add dependencies unless necessary.
- Preserve existing tests and fix regressions caused by the redesign.
- Maintain backward compatibility where practical during migration.
- Do not claim functionality that is not actually implemented.

Because this project is being developed with limited agent/token budget, implementation should be incremental and verification-driven.