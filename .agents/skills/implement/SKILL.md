---
name: implement
description: Context-first implementation workflow - update governing context/ docs (PRD/SPEC/ADR/CONTRACT) and validate acceptance criteria before writing code, then plan, test, implement, and run quality checks. Use when the user asks to implement a CLI feature, build new behavior, or make non-trivial changes that should be governed by a SPEC. Trigger phrases include "implement", "build". Skip for trivial fixes (typos, one-liners) that aren't governed by a SPEC.
user-invocable: true
---

# Context-First Implementation

You are a software engineer implementing features and changes using a **context-first approach**: documentation precedes planning, which precedes implementation.

## Mandatory Process

Follow these steps **in order** for every user instruction.

### Step 1: Plan Context Update

1. **Read `context/CONTEXT.md`** — understand document types, Decision Matrix, and rules.
2. **Analyze the instruction** — determine which context documents need updating:
   - New user-facing capability? -> PRD first, then SPEC
   - CLI behavior change? -> SPEC update or new SPEC
   - Architecture change? -> ADR required
   - Telemetry directory, schema, CLI surface, or integration contract change? -> CONTRACT update
3. **Propose context changes** — list documents to create/update with brief rationale.
4. **Get user approval** before proceeding.

### Step 2: Update Context Documents

1. Create or update the identified documents following `context/CONTEXT.md` rules.
2. Use the appropriate templates from `context/<type>/<TYPE>.md`.
3. Ensure proper frontmatter, cross-references, and validation per `context/CONTEXT.md`.
4. Treat the context update as a conceptually atomic change before moving to implementation.

> Consider delegating this to the `context-engineer` skill — it owns the rules and validators.

### Step 3: Validate Acceptance Criteria

Before planning implementation, verify that the acceptance criteria are complete and traceable. If not, stop and notify the user about the missing or incomplete acceptance criteria.

**Launch a sub-agent** to perform this analysis concurrently when possible.

**Sub-agent task — AC Coverage Analysis:**

1. **Read the SPEC** driving this work (from `context/spec/`).
2. **Extract all Functional Requirements** — collect every `FR-N` identifier.
3. **Extract all Acceptance Criteria** — collect every `AC-N` identifier and its `(Verifies FR-N)` reference.
4. **Compute coverage** — identify which FRs have at least one corresponding AC and which do not.
5. **Check format** — verify ACs use Given/When/Then or structured checklist format.
6. **Report findings** — present a coverage summary to the user.

**Gate behavior:**

| Development context | Gaps found? | Action |
|---------------------|-------------|--------|
| SPEC-driven (instruction references or creates a file under `context/spec/`) | Yes | **Block** — do NOT proceed to Step 4. Guide the user to fill the gaps in the SPEC's Acceptance Criteria section. Update the SPEC and re-validate before continuing. |
| SPEC-driven | No | Proceed to Step 4 |
| Not SPEC-driven (ad-hoc change, trivial fix) | Yes | **Warn** — note the gaps, recommend updating the SPEC later, then proceed to Step 4 |
| Not SPEC-driven | No | Proceed to Step 4 |

> **Why block?** A SPEC with incomplete acceptance criteria is an incomplete contract. Implementing against it produces code that cannot be reliably verified. Filling the gaps *before* coding is cheaper than discovering them during review or testing.

### Step 4: Create Implementation Plan

Based on the now-updated and validated context documents:

1. **Break down the work** into discrete, testable tasks.
2. **Identify affected files** — list all files to create/modify.
3. **Define the order** — dependencies between tasks; **promote parallel work where possible**.
4. **Estimate scope** — flag if scope seems too large for a single session.
5. **Present plan to user** for approval when meaningful.

This step is well suited to fan out on parallel agents.

### Step 5: Design and Implement Test Cases (TDD)

Before writing implementation code, create tests derived from acceptance criteria:

1. **Map ACs to test cases** — each acceptance criterion from the SPEC becomes one or more test cases, preserving the `AC-N` reference (e.g., `test_ac1_invalid_rrule_returns_400`).
2. **Fill coverage gaps** with additional tests for:
   - Edge cases and boundary conditions from `## Edge Cases & Failure Modes`
   - Invariants from `## Invariants`
3. **Implement tests** following project testing conventions (see `AGENTS.md` and `tests/`).
4. **Verify tests fail** — confirms tests are actually testing the new functionality.

> When a SPEC has verification hints on its acceptance criteria, treat them as starting points for test-tooling choices.

### Step 6: Implement

Execute the approved implementation plan:

1. Work through tasks in dependency order.
2. Follow project coding standards from `AGENTS.md`: single-file CLI, strict typing, friendly failures, just recipes, and the Collector file boundary.
3. Validate each change compiles/imports cleanly.
4. Cross-reference implementation back to context docs where helpful.

This step is well suited to fan out on parallel agents.

### Step 7: Run Tests

Validate the implementation against the test cases:

1. **Run the relevant test suite** created in Step 5. Prefer `just otelq-test` for the full suite; use a focused `uv run --with pytest --with "duckdb==1.5.3" pytest <path> -v` command while iterating when useful.
2. **Verify all tests pass** — implementation satisfies requirements.
3. **Check coverage** — ensure critical paths are tested.
4. **Fix any failures** — iterate until tests pass.
5. **For instrumentation-touching work**, close the loop: bring up the Collector (`just otel-up`), reproduce, then inspect the captured traces/logs/metrics with `just otelq ...` commands.

### Step 8: Lint, Format, and Typecheck

Run the project quality checks and fix any issues.

1. **`uvx pyright`** - strict type check; expected result is 0 errors / 0 warnings / 0 informations.
2. **`just lint`** - Ruff lint check.
3. **`just otelq-test`** - full pytest suite with the pinned DuckDB dependency.
4. **Manual context validation** - if context/ docs changed, re-check frontmatter, references, timestamps, and acceptance-criteria coverage against `context/CONTEXT.md`.

If any check fails, correct the problem and re-run until clean.

> **Why a dedicated step?** Pre-commit hooks catch the same issues, but failing at commit time forces a context switch back to fixing. Running these checks as a final implementation step avoids commit-fix-recommit cycles.

## When to Skip Steps

| Scenario | Skip To | Rationale |
|----------|---------|-----------|
| Pure documentation request | Step 1 only | No implementation needed |
| Context already up-to-date (user confirms) | Step 3 | Still validate ACs before coding |
| Context and ACs already validated (user confirms) | Step 4 | ACs are complete and traceable |
| Trivial fix (typo, one-liner) not governed by a SPEC | Step 6 with brief note | Over-process kills velocity on small tasks |
| Tests already exist for the feature | Step 6 | Existing tests imply ACs were already validated |

## Key Principles

- **Context is truth** — implementation follows documentation, not the reverse.
- **No orphan code** — significant features must have corresponding PRD/SPEC.
- **ACs gate implementation** — SPEC-driven work requires complete, traceable acceptance criteria before coding begins.
- **Fail fast** — if context is unclear or contradictory, ask before implementing.
- **Atomic commits** — each step should leave the repo in a valid state.

## Example Usage

User: "Add a command that shows the slowest spans for one service"

1. **Context**: Read `context/CONTEXT.md` -> propose `SPEC-service-slow-spans.md`, update a CONTRACT if the CLI surface or telemetry schema changes.
2. **Update docs**: Create SPEC with functional requirements, acceptance criteria, command behavior, and examples.
3. **Validate ACs**: Sub-agent analyzes SPEC → FR-1 through FR-4 each have ACs, coverage complete → proceed.
4. **Plan**: List `otelq.py`, relevant context docs, and tests.
5. **Test cases**: Derive tests from ACs, such as `test_ac1_service_filter_returns_slowest_spans` and `test_ac2_unknown_service_reports_no_matches`.
6. **Implement**: Add the CLI behavior following `AGENTS.md` and the relevant context docs.
7. **Run tests**: Execute test suite, verify all tests pass.
8. **Quality checks**: `uvx pyright`, `just lint`, and `just otelq-test` all clean.
