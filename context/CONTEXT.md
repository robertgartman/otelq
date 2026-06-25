---
doc_type: context_guide
purpose: >
  Define and distinguish the authoritative context document types used in this repository,
  enabling correct document classification and placement by AI and humans.
authoritative: true
stability: stable
status: active
decision_scope: documentation_system
audience:
  - ai
  - engineering
change_policy: >
  Changes require careful review, as this document defines routing rules for all other context documents.
must_not_contain:
  - product_requirements
  - feature_behavior
  - architectural_decisions
  - implementation_details
retrieval_priority: high
created: 2026-06-23
last_updated: 2026-06-25
---
# Context Documentation Guide

These subfolders contain the **authoritative, version-controlled context** for the **otelq** project — the OpenTelemetry query CLI and dev-Collector tooling. This guide defines and distinguishes the context document types used in this repository so that AI agents and humans classify and place documents correctly.
Each document type serves a distinct purpose.
**A document must answer exactly one question from the Decision Matrix.** If content spans multiple questions, split into separate documents linked via `related_documents`.

---


## Frontmatter Standards

All context documents **must** include YAML frontmatter with these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `doc_type` | Yes | Document classification (see valid values below) |
| `authoritative` | Yes | Whether this document is a source of truth |
| `stability` | Yes | How often this changes (see valid values below) |
| `status` | Yes | Document lifecycle (see valid values below) |
| `created` | Yes | Creation date in `YYYY-MM-DD` format |
| `last_updated` | Yes | Last modification date in `YYYY-MM-DD` format |
| `decision_scope` | Yes | Domain this document governs (see valid values below) |
| `audience` | Yes | Intended consumers of this document (see valid values below) |
| `must_not_contain` | Yes | List of content categories forbidden in this document type |
| `related_documents` | No | List of related document identifiers (format: `TYPE-name`, e.g., `PRD-feature-name`) |
| `supersedes` | No | (ADRs only) Identifier of the ADR this replaces |
| `superseded_by` | No | (ADRs only) Identifier of the ADR that replaces this |
| `retrieval_priority` | No | AI retrieval hint (see valid values and guidance below) |
| `ai_summary` | No | Single-sentence (max 150 chars) machine-readable summary of the document |
| `semantic_tags` | No | List of keywords for embedding similarity and search (e.g., `[authentication, oauth, security]`) |
| `depends_on` | No | List of hard dependencies (documents that must exist for this to be valid) |
| `version` | No | (Contracts only) Semantic version of the interface (e.g., `"1.0"`, `"2.1"`) |
| `purpose` | No | Brief description of the document's intent (used for context guides and READMEs) |
| `change_policy` | No | Guidance on how/when this document should be modified |

### Valid Field Values

| Field | Valid Values |
|-------|--------------|
| `doc_type` | `prd`, `spec`, `adr`, `contract`, `context_guide` |
| `stability` | `stable` (rarely changes), `evolving` (changes with features) |
| `status` | `draft` (work in progress), `active` (authoritative), `deprecated` (no longer valid), `superseded` (replaced) |
| `decision_scope` | `documentation_system`, `product`, `feature`, `architecture`, `interface` |
| `audience` | `ai`, `engineering`, `product` (use list format: `[ai, engineering]`) |
| `retrieval_priority` | `high`, `normal`, `low` (see guidance below) |

### Retrieval Priority Guidance

Use `retrieval_priority` to help AI agents prioritize which documents to read first:

| Priority | When to Use | Examples |
|----------|-------------|----------|
| `high` | Active constraints, safety rules, critical interfaces, breaking changes | Active ADRs, critical contracts |
| `normal` | Standard feature documentation, typical specs | Most SPECs, PRDs |
| `low` | Background context, historical reference, rarely needed | Deprecated docs, detailed appendices |

When `retrieval_priority` is omitted, AI agents **should** treat it as `normal`.

---

## Keyword Conventions

This document uses RFC 2119 terminology:
- **must** / **must not** — absolute requirement or prohibition
- **should** / **should not** — recommended but may be ignored with good reason
- **may** — truly optional

---

## Cross-Document References

When referencing other context documents, use relative markdown links:

```markdown
[TYPE-identifier](relative/path/to/file.md)
```

**Examples:**
- `[PRD-user-authentication](../prd/PRD-user-authentication.md)`
- `[ADR-001-hexagonal-architecture](../adr/ADR-001-hexagonal-architecture.md)`
- `[SPEC-search-ranking](../spec/SPEC-search-ranking.md)`

**Linking expectations:**
- SPECs **should** reference their originating PRD in the Purpose section
- ADRs **should** reference affected CONTRACTs or SPECs in the Consequences section
- CONTRACTs **may** reference ADRs that explain interface design decisions

**`related_documents` format:**
Use the document identifier without file extension: `TYPE-name`
```yaml
related_documents:
  - PRD-user-authentication
  - ADR-001-hexagonal-architecture
  - SPEC-session-management
```

AI agents **should** validate that referenced documents exist before processing.

---

## AI Processing Guidelines

This section provides explicit guidance for AI agents consuming and maintaining these documents.

### Reading Priority by Task Type

**For implementation tasks** (writing code):
1. Read `CONTEXT.md` first for routing rules
2. Read relevant ADRs for architectural constraints
3. Read relevant CONTRACTs for interface requirements
4. Read relevant SPECs for behavioral requirements
5. Read PRDs only if business context is unclear

**For feature design tasks** (planning new capabilities):
1. Read `CONTEXT.md` first for routing rules
2. Read relevant PRDs for product intent
3. Read existing SPECs for patterns and conventions
4. Read ADRs to understand existing constraints

**For documentation tasks** (creating/updating context):
1. Read `CONTEXT.md` first (this document)
2. Read the relevant template for the document type
3. Read `related_documents` from similar existing documents

### Document Precedence (Conflict Resolution)

When documents contain contradictory information, resolve using this precedence:

```
ADR > CONTRACT > SPEC > PRD
```

**Rationale:**
- **ADRs** record architectural decisions that constrain all downstream documents
- **CONTRACTs** define stable interfaces that SPECs must respect
- **SPECs** define behavior within the bounds set by contracts
- **PRDs** express intent that may evolve as constraints are discovered

If a lower-precedence document contradicts a higher-precedence one, the lower document is **stale** and should be updated or flagged.

### Validation Requirements

Before creating or updating any document, AI agents **must**:

1. **Verify references** — All entries in `related_documents` and `depends_on` must point to existing documents
2. **Check content boundaries** — Content must not include categories listed in `must_not_contain`
3. **Update timestamps** — Set `last_updated` to the current date when modifying content
4. **Validate frontmatter** — All required fields must be present with valid values (see tables above)
5. **Check acceptance-criteria coverage** (SPECs only) — Every numbered requirement (e.g., FR-1, RC-1) must be referenced by at least one acceptance criterion (e.g., "Verifies FR-1")

### Staleness Detection

A document may be stale when:

| Signal | Affected Document Types |
|--------|------------------------|
| Code changes behavior that contradicts a SPEC | SPEC |
| New ADR supersedes constraints in existing docs | CONTRACT, SPEC |
| Interface version bump in code but not in CONTRACT | CONTRACT |
| `last_updated` > 6 months and `stability: evolving` | All except ADR |
| Referenced documents have been superseded | Any with `related_documents` |

When staleness is detected, AI agents **should** either update the document or flag it for human review.

### Document Creation Workflow

When creating new documents:

1. **Determine document type** using the Decision Matrix below
2. **Copy the template** from the appropriate folder
3. **Generate frontmatter** with all required fields
4. **Populate content** following the template structure
5. **Add cross-references** to related existing documents
6. **Validate** using the requirements above

---

## Document Lifecycle Rules

### When to CREATE a new document
- New feature or capability → new PRD, then SPEC
- Significant architectural decision → new ADR (never modify accepted ADRs)
- New interface boundary → new CONTRACT

### When to UPDATE an existing document
- Clarifying existing scope → PRD, SPEC
- Adding edge cases discovered during implementation → SPEC
- Updating compatibility notes → CONTRACT
- Correcting factual errors → any document type

### Never update
- **Accepted ADRs** — Updates are allowed if they **amend** the ADR; otherwise, create a new ADR that supersedes the old one
- **Deprecated documents** — archive instead of modifying

### Deprecating or superseding documents
When a document is deprecated or superseded:
1. Set `status: deprecated` or `status: superseded` in frontmatter
2. Add `superseded_by` field pointing to the replacement (if applicable)
3. Move the file to `context/archive/`
4. Preserve the original filename for traceability
5. Preserve the original `doc_type` for traceability

---

## 1. PRD (Product Requirements Document)

**Purpose:**  
Define **why** we are building something and **what success means**.

**Contains:**
- Problem statement
- Goals and non-goals
- Target users
- Success metrics
- High-level constraints (business, legal, cost, UX)

**Does NOT contain:**
- Implementation details
- APIs or schemas
- Step-by-step behavior
- Technical solutions

**Decision rule:**
Use a PRD when defining or changing **product intent or scope**.

**Target folder:**
All PRD documents **must** be placed in `context/prd/`.

**Naming standard:**
PRD files **must** follow the pattern: `PRD-<feature-name>.md`
Example: `PRD-user-authentication.md`, `PRD-metrics-export.md`

**Template:**
All PRD documents **must** follow the template [PRD.md](prd/PRD.md).

---

## 2. Specs (Feature / Behavior Specifications)

**Purpose:**  
Define **exact system behavior** so it can be implemented and tested.

**Contains:**
- Functional requirements
- Rules and invariants
- Edge cases and failure modes
- Acceptance criteria (each tracing to at least one functional requirement)
- Expected inputs and outputs

**Does NOT contain:**
- Product vision or business rationale
- Architectural decisions (see ADRs)
- **External** API or data schemas (see Contracts)

> **Note:** Internal data structures that are not exposed as interfaces **may** be documented in SPECs.

**Decision rule:**
Use a Spec when describing **what the system must do** in precise terms.

**Target folder:**
All Spec documents **must** be placed in `context/spec/`.

**Naming standard:**
Spec files **must** follow the pattern: `SPEC-<feature-name>.md`
Example: `SPEC-search-ranking.md`, `SPEC-session-management.md`

**Template:**
All Spec documents **must** follow the template [SPEC.md](spec/SPEC.md).

---

## 3. ADRs (Architecture Decision Records)

**Purpose:**  
Record **irreversible or high-impact technical decisions** and their rationale.

**Contains:**
- Decision being made
- Context and constraints
- Considered alternatives
- Chosen option and consequences

**Does NOT contain:**
- Feature requirements
- Implementation walkthroughs
- Reversible or trivial choices

**Decision rule:**
Use an ADR when a decision **constrains future design or implementation**.

**Target folder:**
All ADR documents **must** be placed in `context/adr/`.

**Naming standard:**
ADR files **must** follow the strict pattern: `ADR-NNN-<short-title>.md`
- `NNN` is a zero-padded sequential number (001, 002, ... 999)
- `<short-title>` is a lowercase, hyphen-separated summary
- Numbers **must** be assigned sequentially without gaps
Example: `ADR-001-hexagonal-architecture.md`, `ADR-015-session-analysis.md`

**Template:**
All ADR documents **must** follow the template [ADR.md](adr/ADR.md).

### ADR Numbering

ADR numbers are sequential, zero-padded (ADR-001, ADR-002, …), assigned without gaps. Before assigning, check the highest existing number in `context/adr/` (and any unmerged branch). ADR/SPEC requirement IDs are append-only. Never modify an accepted ADR — supersede it with a new one.

---

## 4. API / Data Contracts

**Purpose:**  
Define **stable interfaces and schemas** between system components.

**Contains:**
- API definitions (e.g. OpenAPI, gRPC)
- Data schemas (e.g. JSON Schema, Protobuf)
- Field meanings and constraints
- Backward-compatibility expectations

**Does NOT contain:**
- Business goals
- Behavioral explanations
- Decision rationale (see ADRs)

**Decision rule:**
Use Contracts when defining **how systems communicate or exchange data**.

**Target folder:**
All Contract documents **must** be placed in `context/contract/`.

**Naming standard:**
Contract files **must** follow the pattern: `CONTRACT-<interface-name>.md`
Example: `CONTRACT-recommendation-api.md`, `CONTRACT-session-schema.md`

**Template:**
All Contract documents **must** follow the template [CONTRACT.md](contract/CONTRACT.md).

---

## Summary Decision Matrix

| Question | Document Type |
|--------|---------------|
| Why are we building this? | PRD |
| What exactly must the system do? | Spec |
| Why was this technical approach chosen? | ADR |
| How do systems exchange data? | API/Data Contract |
| How should documents be classified and organized? | Context Guide |

---

## Folder and Naming Quick Reference

| Document Type | Target Folder | Naming Pattern | Strict? |
|--------------|---------------|----------------|---------|
| PRD | `context/prd/` | `PRD-<feature-name>.md` | Yes |
| Spec | `context/spec/` | `SPEC-<feature-name>.md` | Yes |
| ADR | `context/adr/` | `ADR-NNN-<short-title>.md` | Yes (sequential) |
| Contract | `context/contract/` | `CONTRACT-<interface-name>.md` | Yes |
| Context Guide | `context/` | `CONTEXT.md` (singleton) | Yes |

---

**Rule of thumb:**
If the document answers more than one of the questions above, it is in the wrong place.

**If content spans multiple categories:**
1. Split the content into separate documents
2. Link them via `related_documents` in frontmatter
3. Each document should answer only ONE question from the matrix above