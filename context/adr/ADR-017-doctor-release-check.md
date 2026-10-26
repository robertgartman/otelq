---
doc_type: adr
authoritative: true
stability: stable
status: active
decision_scope: architecture
audience:
  - ai
  - engineering
must_not_contain:
  - feature_requirements
  - implementation_walkthroughs
  - reversible_decisions
created: 2026-10-25
last_updated: 2026-10-25
related_documents:
  - ADR-016-cli-carries-versioned-agent-skill
  - ADR-007-dual-collector-standalone-and-integrated
  - ADR-012-exit-codes-as-public-contract
  - ADR-002-pep723-uv-single-file-distribution
  - ADR-003-duckdb-otlp-extension-pin-governance
  - SPEC-otelq-cli
  - PRD-otelq
supersedes: null
superseded_by: null
retrieval_priority: high
ai_summary: "doctor, and only doctor, makes one bounded, opt-out request for otelq's latest release, so a project pinned to an old otelq learns an upgrade exists."
semantic_tags:
  - otelq
  - doctor
  - release-check
  - upgrade
  - network
  - privacy
  - versioning
---

# ADR-017 — `doctor` Checks for a Newer otelq Release

## Context

[ADR-016](ADR-016-cli-carries-versioned-agent-skill.md) pins a project's agent
skill to the otelq version that installed it, and gives one command to move
otelq and the skill forward together. Pinning makes every run reproducible, but
it also hides upgrades: the agent always runs the pinned CLI, and `doctor`
compares the installed skill with the CLI that is running, which is that same
pinned version. Its skill check therefore reports a healthy state indefinitely,
even after newer releases ship. Nothing tells a user that an upgrade exists.

Two constraints frame the fix:

- **otelq is local and CLI-only.** Until now otelq's only network access was
  DuckDB's one-time download of the `otlp` extension
  ([ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md)). The CLI is fast to invoke because each query is a local read
  ([ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md),
  [ADR-002](ADR-002-pep723-uv-single-file-distribution.md)). Telemetry never
  leaves the machine.
- **`doctor` is read-only**
  ([ADR-007](ADR-007-dual-collector-standalone-and-integrated.md)). It reports on
  state and creates nothing.

## Decision

**`doctor` makes one bounded request for otelq's latest published release and
reports the result as a diagnostic.** The rules:

1. **Only `doctor`.** No query command, meta command, or bounded wait ever makes
   this request. The release check runs when a person or an agent asks for a
   diagnosis, never in the query loop.
2. **Metadata in, nothing out.** The request reads the public release metadata
   of the otelq package from the Python package index. It sends no telemetry, no
   project path, no query, and no identifier beyond what any package download
   sends.
3. **Bounded and friendly.** The request has a short timeout. An unreachable
   index, a timeout, or an unexpected answer is reported as an informational
   row, never an error, a traceback, or a change to `doctor`'s exit code
   ([ADR-012](ADR-012-exit-codes-as-public-contract.md)).
4. **Opt-out.** An environment variable turns the check off; the row then says
   it is disabled, and no request is made.
5. **Nothing is written.** The result is not cached. `doctor` stays read-only,
   and a repeat run simply asks again.
6. **Report, never act.** `doctor` names the upgrade command; it does not run
   it. The agent skill instructs agents to tell the user about an available
   upgrade and leave the decision to them, because upgrading changes files the
   project commits.

## Alternatives Considered

- **Check on every invocation, or once a day from any command.** Rejected. It
  puts network latency, or a cache write, into the query loop that the CLI-only
  design exists to keep instant.
- **Cache the answer in a user cache directory.** Rejected. It would make
  `doctor` write outside the project, contradicting its read-only role, to save
  one small request on a command that runs rarely.
- **Let the agent discover upgrades itself**, for example by asking the package
  index. Rejected. The result would depend on which agent and which tools are
  present, and nothing would guarantee it happens.
- **External bots that open upgrade pull requests.** Kept as optional for teams
  that run them. A plain pin bump without regenerating the skill text is reported
  by `doctor` as a hand edit, so a bot needs to run the install command, which
  only some setups allow.
- **No check.** Rejected. Under ADR-016's pinning, users would never learn that
  a release exists.

## Consequences

- **otelq gains a second kind of network request**, beside the one-time
  extension download, in exactly one command. The privacy promise is restated precisely: telemetry never leaves
  the machine, and `doctor` may fetch public release metadata unless turned off.
- **Offline and air-gapped use stays clean.** The check degrades to an
  informational row; setting the opt-out removes the request entirely.
- **`doctor` can take slightly longer**, bounded by the timeout, when the index
  is slow or unreachable.
- **Upgrades stay deliberate.** Discovery is automatic; the upgrade itself
  remains one human-approved command and one commit, per ADR-016.
- **Behavior is specified in [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md)**
  (FR-51, FR-52); this ADR governs only whether and where otelq may reach the
  network for release discovery.
