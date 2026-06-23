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
created: 2026-06-23
last_updated: 2026-06-23
related_documents:
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-004-collector-in-docker-bind-mount
  - CONTRACT-telemetry-directory
  - SPEC-otelq-cli
supersedes: null
superseded_by: null
ai_summary: "otelq is a pure consumer of the telemetry dir; it supports both a bundled (standalone) Collector and a project-owned (integrated) one, with `collector-config` as the generated reference-producer config and `doctor` as the conformance check. It manages only producers it owns."
semantic_tags:
  - otelq
  - opentelemetry
  - collector
  - producer-agnostic
  - integration
  - collector-config
  - doctor
  - observability
---

# ADR-007 — Dual Collector Setup: Standalone and Integrated Producers

## Context

[ADR-004](ADR-004-collector-in-docker-bind-mount.md) introduced the capture
Collector as an **opt-in Compose service that otelq ships and runs**. That framing
fits a project with no telemetry stack, but it is the exception, not the rule: in a
real project the OpenTelemetry Collector is already part of the solution
architecture, owned and lifecycle-managed by that project. Having otelq stand up
and tear down a *second* Collector there is redundant at best and conflicting at
worst.

The interface, however, is already producer-agnostic.
[CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)
guarantees that "any producer that writes files matching this layout and any
consumer that reads them interoperate without further coordination; the directory
is the sole point of agreement," and otelq reads whatever directory `--dir` names
([SPEC-otelq-cli](../spec/SPEC-otelq-cli.md)). The gap is not architectural — it is
ergonomic: a project integrating its own Collector needs the exact file-export
settings to add, and a way to confirm the result conforms.

## Decision

Treat otelq as a **pure consumer** of the telemetry directory and support **two
producer topologies** behind the one contract:

- **Standalone** — otelq's bundled, profile-gated Collector (ADR-004). For a
  project with no Collector, or a demo. otelq owns this producer's lifecycle
  (`just otel-up` / `otel-down` / `otel-clean`).
- **Integrated** — a *separate* project's own Collector (elsewhere on the same
  host), extended with otelq's three `file/<signal>` exporters writing the
  contract layout. This is the normal case for an instrumented project. The
  integration is driven **from the otelq repo onto the target project** (addressed
  by its absolute path): otelq is the tool, the target project is operated on — not
  the reverse. The `integrate-collector` skill encodes this direction and asks for
  the target path before touching anything.

Two CLI affordances make the integrated path reliable:

- **`otelq collector-config`** prints the reference file-export fragment
  (exporters + pipeline wiring + bind-mount and `-contrib`-image guidance). It is
  **generated from otelq's own pinned constants**, making otelq the single source
  of truth for the reference producer; a hand-copied fragment would fork the
  contract and silently drift. A test asserts the generated values match the
  shipped `otel-collector-dev.yaml`.
- **`otelq doctor --dir <path>`** validates a telemetry directory against the
  contract (files present, valid OTLP/JSON, correct top-level signal per file) and
  exits non-zero on failure, giving humans and agents a deterministic
  "is the wiring correct?" check.

**Lifecycle boundary (load-bearing): otelq manages only producers it owns.** In
integrated mode otelq never starts, stops, or cleans the project's Collector, and
its only writes under the telemetry root remain the consumer-owned `.otelq-cache/`
subtree. The `just otel-*` recipes — `otel-clean` in particular, which truncates
the active `.jsonl` files — are **standalone-only** and must not be run against an
integrated directory.

## Alternatives Considered

- **Ship a static example fragment to copy.** Rejected: a copied snippet drifts
  from the contract the moment rotation thresholds, paths, or framing change.
  Generating the fragment from otelq's constants keeps producer and consumer in
  lockstep by construction.
- **Have otelq spawn/manage the project's Collector.** Rejected: it couples otelq
  to another component's lifecycle and configuration, the opposite of the
  decoupled, directory-only contract. otelq orchestrating a Collector it does not
  own invites conflicting restarts and destructive resets.
- **Mandate the bundled Collector for everyone.** Rejected: it forces a redundant
  second Collector onto projects that already run one, and contradicts the
  contract's explicit producer-independence guarantee.

## Consequences

- otelq's normal deployment story is **consumer-only**: point `--dir` at the
  telemetry directory a project's existing Collector already writes. The bundled
  Collector becomes the zero-stack fallback, not the assumed default.
- The reference producer config has a **single source of truth** in otelq;
  `collector-config` and `otel-collector-dev.yaml` cannot diverge without failing
  a test.
- `doctor` gives close-the-loop agents a contract-conformance gate with a
  meaningful exit code, turning "did integration work?" into one command.
- The standalone-only scope of the `just otel-*` recipes must be stated wherever
  integration is documented, because `otel-clean` against a project-owned dir
  would destroy that project's telemetry.
- Adding a producer topology is additive to the contract (no interface change);
  ADR-004's bundled Collector remains valid and unchanged as the standalone
  producer.
