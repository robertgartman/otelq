---
doc_type: spec
authoritative: true
stability: evolving
status: draft  # draft | active | deprecated | superseded
decision_scope: feature
audience:
  - ai
  - engineering
must_not_contain:
  - product_vision
  - architectural_rationale
  - api_schema_definitions
created: YYYY-MM-DD
last_updated: YYYY-MM-DD
related_documents: []  # e.g., [PRD-feature-name, ADR-001-decision]
---

> **Naming Standard:** This file **must** be named following the pattern: `SPEC-<feature-name>.md`
> Example: `SPEC-search-ranking.md`, `SPEC-session-management.md`

# Feature / Behavior Specification

## Purpose
Define **exact system behavior** so it can be implemented and tested.

## Scope
(What is covered by this spec.)

## Functional Requirements
(Numbered, testable rules.)

## Edge Cases & Failure Modes
(What happens when things go wrong.)

## Acceptance Criteria

(Verifiable conditions that confirm each Functional Requirement is satisfied.)

**Format guidance:** Use Given/When/Then (BDD-style) or a structured checklist. Each criterion should be independently testable. An optional *Verification hint* **may** be added to suggest tools, commands, or approaches for verifying the criterion.

> - Each Functional Requirement **must** have at least one corresponding acceptance criterion.
> - Acceptance criteria **must** reference the Functional Requirement they verify (e.g., "Verifies FR-1").
> - Critical edge cases and failure modes **should** have at least one acceptance criterion.
> - Verification hints are suggestions, not mandates — they **may** become outdated independently of the criterion itself.

> **AI Agent Note:** If acceptance criteria cannot be fully derived from the available context, the AI agent **must** enter interview mode — prompting the user with targeted questions to elicit the missing criteria rather than guessing or leaving gaps. Continue until every numbered requirement has at least one verifiable AC.

**Example format:**
> - **AC-1** (Verifies FR-1): Given \<precondition\>, when \<action\>, then \<expected outcome\>.
>   *Verification hint: \<tool, script, or approach\>*

### Examples
(Concrete input/output examples that illustrate acceptance criteria or edge cases.)

## Invariants
(Rules that must always hold.)

> Do NOT include business rationale or architectural decisions.
> Minimize the use of code examples. **Be less prescriptive and more declarative.**
> Every numbered requirement (e.g., FR-1, RC-1) **must** be traceable to at least one AC. An AC section that does not cover all numbered requirements is invalid.
