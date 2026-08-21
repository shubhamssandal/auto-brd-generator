# Auto-BRD Generator — Product Requirements Document (PRD)

## Product Overview
Auto-BRD Generator converts meeting discussions and transcripts into a structured Business Requirements Document (BRD). It supports manual transcript input, transcript-file upload, Google Meet, and Microsoft Teams as transcript sources.

The next product phase extends the workflow into Jira so users can convert approved requirements into a reviewable Jira work plan and create approved work items in Jira.

## Problem
Business and product teams often discuss requirements, manually create a BRD, then manually convert requirements into Jira tickets. This creates duplicate work, inconsistent requirements, missed decisions, and weak traceability.

## Product Goal
Meeting / Transcript → Transcript Processing → Structured BRD → User Review → Jira Work Plan → User Review → Jira Issue Creation

## Target Users
- Business Analysts
- Product Analysts
- Product Managers
- Project Managers
- Delivery / Engineering teams using Jira

## Current Capabilities
- Manual transcript paste
- Upload transcript file (.txt)
- Google Meet connection and transcript retrieval flow
- Microsoft Teams integration
- Transcript processing
- Structured BRD generation
- OAuth/provider architecture for external transcript providers

## Jira Scope
### Connect Jira
Connect Jira Cloud securely using OAuth.

### Discover Jira Resources
Retrieve accessible Jira sites, projects, issue types, relevant metadata, and required fields. Do not assume every project uses the same hierarchy.

### Generate Jira Work Plan
From an approved or selected BRD, generate a proposal:

Epic
├── Story / Task
│   ├── Description
│   ├── Acceptance Criteria
│   └── Subtasks
└── Story / Task
    ├── Description
    ├── Acceptance Criteria
    └── Subtasks

Generation must not automatically create Jira issues.

### Review Before Creation
Allow users to edit, delete, add, select, and review parent relationships before creation.

### Create Jira Issues
Only explicitly approved items are created. Return issue keys and links.

## Future Scope
- Requirement-change detection from future transcripts
- Comparison against existing BRDs
- Identification of affected Jira issues
- Proposed, user-approved Jira updates
- Jira status visibility

## Core User Journey
1. User provides or retrieves a transcript.
2. System processes the transcript.
3. System generates a BRD.
4. User reviews the BRD.
5. User connects Jira.
6. User selects Jira site and project.
7. System generates a Jira work plan.
8. User reviews and edits it.
9. User explicitly confirms issue creation.
10. System creates selected Jira issues.
11. System returns issue references and links.

## Product Principles
- User approval before external side effects
- AI proposes; user decides
- Minimum required permissions
- No hardcoded Jira assumptions
- Clear error messages
- Traceability
- Preserve existing functionality

## Success Criteria
A user can connect Jira, select a site/project, generate a reviewable plan, edit it, create approved issues, and see created issue references without breaking existing functionality.

## Out of Scope for Initial Jira Release
- Automatic issue creation without review
- Automatic production changes from transcripts
- Full two-way synchronization
- Automatic workflow transitions
- Universal support for every custom Jira configuration without metadata validation
