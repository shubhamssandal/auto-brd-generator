# Auto-BRD Generator from Meeting Notes

## Project Goal

Build a portfolio project that demonstrates Business Analyst and Product Analyst skills.

The application converts raw meeting notes into a structured draft Business Requirements Document (BRD).

The project should demonstrate:

- Requirements analysis
- Requirement extraction
- Ambiguity identification
- Requirement traceability
- Scope identification
- Stakeholder analysis
- Responsible use of an LLM

The project is being built as a learning project. The implementation should remain simple enough to understand and explain clearly during Business Analyst, Product Analyst, and technical interviews.

---

# Core Principle

The application must never present AI-inferred information as a confirmed requirement.

The original meeting notes are the source of truth.

Only information explicitly supported by the source meeting notes should be classified as a **Confirmed Requirement**.

If information is inferred but not explicitly confirmed, it must be clearly flagged as an **Assumption**.

If information is unclear, incomplete, unresolved, or requires stakeholder follow-up, it must be classified as an **Open Question**.

Do not invent:

- Stakeholders
- Requirements
- Technical constraints
- Business goals
- Measurable targets
- Business rules
- Acceptance criteria
- Project scope

that are not supported by the source meeting notes.

Uncertainty must be preserved rather than resolved by inventing information.

---

# Evidence and Traceability Rule

Every confirmed functional or non-functional requirement must include supporting evidence from the original meeting notes.

The supporting evidence should be an exact quote or a clearly identifiable statement from the source notes.

Each confirmed requirement should be traceable back to its supporting evidence.

If sufficient supporting evidence cannot be identified, the item must not be classified as a Confirmed Requirement.

Instead, it should be classified as an Assumption, Open Question, Background Information, or another appropriate category.

The application must not blindly trust LLM output.

---

# BRD Section Structure

The generated BRD should contain the following sections.

## 1. Project Overview

Include only information available in the source notes:

- Project name
- Date
- Prepared by
- Stakeholders

If information is unavailable, use:

`Not specified in source notes`

Do not invent missing information.

---

## 2. Business Objectives

Identify:

- The business problem being addressed
- Any business objective explicitly mentioned
- Any measurable business goal explicitly mentioned

Do not invent measurable targets, KPIs, or objectives.

---

## 3. Scope

Separate scope into two sections.

### In Scope

Include:

- Features explicitly requested
- Work explicitly included
- Requirements clearly included in the current project

### Out of Scope

Include only items that are:

- Explicitly excluded
- Deferred
- Parked for later
- Identified as belonging to a separate project
- Explicitly planned for a future version

Do not assume that an unmentioned feature is out of scope.

---

## 4. Stakeholders

For each stakeholder mentioned, identify:

- Name
- Role, only if explicitly mentioned
- Their stated interest
- Their concern
- Their requirement
- Their responsibility, only if explicitly mentioned

Do not invent roles, interests, or responsibilities.

---

## 5. Functional Requirements

Create a numbered list:

- FR-1
- FR-2
- FR-3

Confirmed functional requirements should be written clearly using the format:

`The system shall...`

Each confirmed requirement must include:

- Requirement ID
- Requirement statement
- Supporting source evidence

Do not create a confirmed functional requirement without supporting evidence.

---

## 6. Non-Functional Requirements

Include constraints related to:

- Performance
- Security
- Usability
- Reliability
- Availability
- Scalability

Only include a non-functional requirement when it is explicitly mentioned or clearly supported by the source notes.

Do not invent standard non-functional requirements simply because they are common in software projects.

Each confirmed non-functional requirement should include supporting evidence.

---

## 7. Assumptions

Include information that may be relevant to the project but is inferred rather than explicitly confirmed.

Every assumption should:

- Be clearly labelled as an Assumption
- State what is being assumed
- Explain why stakeholder confirmation is required

Assumptions must never be presented as confirmed requirements.

---

## 8. Open Questions

Include information that is:

- Unclear
- Incomplete
- Unresolved
- Missing necessary detail
- Requiring stakeholder follow-up

Do not answer an Open Question by inventing information.

---

## 9. Acceptance Criteria

Generate clear and testable conditions only when they can reasonably be supported by a Confirmed Requirement.

Do not invent detailed business rules that are absent from the source notes.

If a requirement does not contain enough information for complete acceptance criteria, identify the missing information as an Open Question or Assumption.

Acceptance criteria should be testable and clearly linked to the relevant requirement where possible.

---

# Classification Rules

When analyzing meeting notes, distinguish between the following categories:

## 1. Confirmed Requirement

A requirement explicitly supported by the source notes.

Must include source evidence.

## 2. Assumption

Information inferred from the notes but not explicitly confirmed.

Must be clearly flagged and require stakeholder confirmation.

## 3. Open Question

Information that is unclear, incomplete, unresolved, or requires follow-up.

Must not be answered using invented information.

## 4. Explicitly Deferred or Out of Scope

A feature or request explicitly deferred, parked, excluded, assigned to another project, or planned for a future version.

## 5. Background Information or Context

Information that provides useful context but is not itself a requirement.

Examples may include:

- Current business problems
- Existing system limitations
- Technical dependencies
- Risks
- Historical information

Do not automatically convert Background Information into a Functional Requirement.

---

# Example Classification Rules

If the meeting notes say:

> Users should have a forgot password option.

This may be classified as a Confirmed Functional Requirement:

> The system shall allow users to reset their password.

Supporting evidence:

> Users should have a forgot password option.

If the meeting notes say:

> We need to think about account security.

The application must not generate:

> The system shall support multi-factor authentication.

Instead, it should create an Assumption or Open Question explaining that specific security requirements have not been defined.

If the meeting notes say:

> Push notifications can be considered later, but are not part of version 1.

The application should classify push notifications as:

> Explicitly Deferred / Out of Scope for Version 1.

It must not classify them as an In Scope Functional Requirement.

---

# Technology Stack

- Python 3.11
- Streamlit for the web application interface
- Gemini API for LLM-powered requirement extraction
- Local text files for sample meeting notes during development
- No database in the MVP
- pytest may be added later for testing
- Streamlit Community Cloud as the deployment target
- Git and GitHub for version control

Keep the technology stack simple unless there is a clear project requirement to add something else.

---

# Planned Application Architecture

The application will eventually follow this flow:

```text
User pastes meeting notes
        ↓
Streamlit user interface
        ↓
Python application logic
        ↓
Gemini API
        ↓
Structured requirement extraction
        ↓
Python validation and evidence checking
        ↓
Classification into:
- Confirmed Requirements
- Assumptions
- Open Questions
- Deferred or Out of Scope Items
- Background Information
        ↓
Structured BRD generation
        ↓
BRD displayed to the user
        ↓
BRD export
        ↓
Explicit BRD approval → PRD → review/edit → approval
        ↓
Explicit PRD approval → Architecture → review/edit → approval
        ↓
Explicit architecture approval → Implementation Plan
(epics → stories → technical tasks) → review/edit → approval
        ↓
Explicit plan approval → mapped onto the selected Jira project's
own issue types → preview → explicit confirmation → Epics /
Stories / Tasks / Subtasks → plan item → issue key mapping
        ↓
Later stages (sprint planning, test cases, test execution,
coding agent) — not implemented yet
```

---

# Architecture Rules

Gemini is responsible for analyzing and extracting information from the meeting notes.

The Python application is responsible for validating and processing the LLM output.

The application must not blindly display all Gemini output as confirmed information.

A Confirmed Requirement must contain supporting evidence from the original meeting notes.

Validation logic should remain simple, transparent, and easy to explain during an interview.

The MVP should prioritize reliability and traceability over unnecessary AI complexity.

Traceability in the later lifecycle stages reuses the identifiers the previous artifact
already minted: a PRD feature names BRD requirement ids, an architecture component names
PRD feature ids, and an implementation-plan story names PRD feature ids and architecture
component ids. No stage introduces a second identifier scheme.

Traceability is enforced differently at each stage, deliberately, and the rule is the same
one every time: *does the untraceable item represent invented scope, or real cross-cutting
work?* A PRD feature that names no BRD requirement is invented product scope and is
dropped. An architecture component that names no PRD feature is *kept* and counted in the
artifact's notes, because an API gateway, a shared auth service or a CI pipeline is
genuinely cross-cutting and dropping it would delete real technical design. An
implementation-plan story that names no PRD feature is invented scope and is dropped, with
a note telling the reviewer to re-add it as a task if it was real technical work; a *task*
that names no architecture component is kept, because repository setup or a release step
touches no single component.

An architecture component's layer (backend, web, mobile) is determined by the section of
the model response it arrived in, never by a layer the model asserts about itself. Only a
component in a generic list has its layer inferred, and the inference is recorded.

The implementation plan is deliberately not tracker-shaped. `jira_models.PlannedIssue` is
bound to a selected Jira project's issue types, hierarchy levels and required fields, so it
cannot exist at this stage of the lifecycle. `implementation_plan_models.ImplementationPlan`
is the tracker-agnostic engineering structure; `implementation_plan_jira.py` maps one onto
the other when a reviewer confirms a delivery. The two models are separate on purpose and
must not be merged.

Delivery into a tracker is one-way and keyed by the plan's own ids. The mapping
`plan item id → Jira issue key` is recorded as issues are created, which is both the
traceability record and the reason a retry cannot duplicate anything: an item that already
has a key is never sent again. A hierarchy the target project cannot represent is reported
and left uncreated rather than flattened onto the wrong parent. Nothing Jira reports may
change the plan, the architecture, the PRD or the BRD, and no per-issue read exists — a
workflow status change is an implementation-status change, never a requirement change.

A model proposal is repaired deterministically rather than re-prompted. Unknown upstream
ids, references to an epic that is not in the plan, self-dependencies, dependencies that
match no story, dependency cycles and priorities outside the vocabulary are all fixed by
ordinary code, and every repair is recorded in the artifact's notes so a reviewer reads
what changed instead of trusting the result. Cycle breaking removes only the closing link,
so the work stays startable.

---

# Planned Development Order

Develop the project in the following stages:

1. Build the basic Streamlit user interface.
2. Define the BRD and extraction data structure.
3. Test the application using sample and mock data.
4. Integrate the Gemini API.
5. Validate extracted requirements against source evidence.
6. Generate and display the structured BRD.
7. Add basic error handling.
8. Add testing.
9. Add BRD export functionality.
10. Deploy the application to Streamlit Community Cloud.

Only build the current stage when specifically instructed.

Do not automatically start implementing future stages.

---

# AI Coding Workflow

Before making any code changes:

1. Read `PROJECT_CONTEXT.md`.
2. Review the relevant existing files.
3. Explain the proposed change in simple language.
4. List exactly which files will be created or modified.
5. Wait for my approval before making changes.

When implementing:

- Work on only one small feature at a time.
- Do not modify unrelated files.
- Do not refactor working code unless necessary.
- Do not add dependencies unless they are needed for the current task.
- Prefer simple and readable code over complex abstractions.
- Do not create unnecessary files or folders.
- Add comments only when they help explain important logic.

After implementing:

1. Explain what changed.
2. Explain how to run or test the change.
3. Explain any new Python, Streamlit, or Gemini API concept introduced.
4. Explain what the next logical development step would be.

---

# LLM Output Rules

When Gemini API integration is added, request structured output.

Gemini should distinguish between:

1. Confirmed Requirement
2. Assumption
3. Open Question
4. Explicitly Deferred or Out of Scope Item
5. Background Information or Context

A Confirmed Requirement should include:

- Requirement statement
- Supporting source evidence
- Classification

The LLM must not classify an item as a Confirmed Requirement without supporting evidence from the source notes.

If the source notes are ambiguous, uncertainty must be preserved rather than resolved by inventing information.

The original source notes are the source of truth.

---

# Code Conventions

Use simple Python that is easy to understand.

Prefer:

- Clear and descriptive variable names
- Small functions with one clear responsibility
- Simple conditional logic
- Simple data structures
- Type hints when they improve readability
- Pydantic models later when structured validation is needed

Avoid unnecessary:

- Complex design patterns
- Deep inheritance
- Excessive classes
- Abstraction layers
- Helper files
- Configuration systems

Do not create a function, class, or file unless it has a clear purpose.

The goal is a simple, maintainable MVP that can be clearly explained during an interview.

---

# Initial Test Scenario

The project will include sample meeting notes containing situations such as:

- A clearly requested feature
- A measurable business problem
- A feature explicitly deferred from version 1
- An item with unclear project ownership
- A technical dependency or risk
- A missing stakeholder decision
- A request explicitly parked for later

The application should correctly distinguish these situations rather than turning every sentence into a Functional Requirement.

---

# Current Development Stage

We are currently building the basic application foundation.

The first version should only:

1. Display the application title: `Auto-BRD Generator`
2. Display a short description explaining that the application converts meeting notes into a structured BRD
3. Allow the user to paste meeting notes into a large text area
4. Display a button labelled `Generate BRD`

The `Generate BRD` button does not need to generate a BRD yet.

Do not add:

- Gemini API integration
- API keys
- Database integration
- Authentication
- Testing framework
- Deployment configuration
- BRD export functionality

until specifically requested.
