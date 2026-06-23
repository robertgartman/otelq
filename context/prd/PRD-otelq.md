---
doc_type: prd
authoritative: true
stability: stable
status: active
decision_scope: product
audience:
  - ai
  - engineering
must_not_contain:
  - implementation_details
  - api_schema_definitions
  - architectural_decisions
  - step_by_step_behavior
created: 2026-06-23
last_updated: 2026-06-23
related_documents:
  - SPEC-otelq-cli
  - ADR-001-host-cli-reads-bind-mounted-files
ai_summary: Why otelq exists — a zero-infrastructure CLI to see and query the OpenTelemetry a local app actually emitted, in the inner loop and in CI, for humans and AI agents alike.
semantic_tags:
  - otelq
  - opentelemetry
  - otlp
  - observability
  - developer-experience
  - inner-loop
  - verification
  - ai-agent
  - cli
---

# otelq

## Purpose

Define **why** `otelq` exists and **what success means** for it.

`otelq` answers a single, recurring question in the local development loop: *"I instrumented my app with OpenTelemetry — what did it actually emit?"* It lets a developer, or an AI coding agent acting on their behalf, run their code and then immediately see and query the traces, logs, and metrics that came out of it — without standing up an observability backend, opening a web UI, or leaving the terminal.

This document captures product intent and the outcomes that define success. It does not define system behavior, command surface, or technical solutions. Behavioral requirements live in [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md). The decision that `otelq` runs as a host-side CLI reading the telemetry files a local Collector writes to a shared location — rather than as a server, daemon, or service of its own — is recorded in [ADR-001-host-cli-reads-bind-mounted-files](../adr/ADR-001-host-cli-reads-bind-mounted-files.md).

## Problem Statement

Adding OpenTelemetry instrumentation to an application is increasingly easy; *seeing the result of that instrumentation, locally, in the moment* is not. The gap shows up in three places.

**1. The inner loop has no fast feedback.** A developer adds a span, an attribute, a log line, or a metric, runs the code, and then has no quick way to confirm what was emitted. The conventional answer is to stand up a full observability stack — Jaeger, SigNoz, Grafana with Tempo and a metrics store — locally. That is heavy: it is slow to start, consumes resources, demands configuration and upkeep, and is wildly disproportionate to the actual question ("did my one change emit the right span?"). Faced with that cost, developers skip verification and instead guess, eyeball raw exporter logs, or discover instrumentation mistakes much later. The feedback loop that should take seconds takes minutes, or never closes at all.

**2. AI coding agents cannot close the loop after a change.** An agent that modifies instrumented code should be able to verify its own work the same way a careful human would: run the code, then confirm from the telemetry that the system behaved as intended — the expected span exists, the error was recorded, the latency is plausible, the right metric moved. Today that confirmation step has no agent-friendly path. A graphical observability UI is unusable to an agent, and parsing raw OTLP by hand is brittle. Without a programmatic way to ask "what did this run emit?", an agent's verification is reduced to inference rather than observation, which is exactly where instrumentation regressions slip through.

**3. CI failures are opaque about telemetry.** When a test that exercises instrumented code fails in CI — or when the telemetry itself is the thing under test — the captured signals are effectively a black box. There is no lightweight, scriptable way to inspect "what did the failing run actually emit, and what errored?" so diagnosis falls back to re-running locally and reproducing by hand.

Underlying all three: the existing tools are observability *platforms*, optimized for long-lived, large-scale, multi-service production monitoring. The local-development question is the opposite — short-lived, single-developer, single-run, and latency-sensitive — and is poorly served by platform-shaped tooling.

## Goals

- **Zero-friction local capture and query.** Make it trivial to capture the OTLP traces, logs, and metrics a local app emits and to query them, with no persistent infrastructure to provision and nothing to keep running beyond an ordinary, stock OpenTelemetry Collector that writes what it receives somewhere `otelq` can read it.
- **Close the loop: act, then verify from telemetry.** Support the full "run the code, then confirm from its telemetry that it behaved" workflow as a first-class outcome, so verification is grounded in what was actually emitted rather than in assumption.
- **Be equally usable by humans and AI agents.** Produce output in both human-readable text and machine-parseable JSON, so a person reading a terminal and an automated agent reading structured output are both well served by the same tool.
- **Stay instant and local.** Keep the time from "the app emitted something" to "I can query it" in the range of seconds, and keep `otelq` itself a fast, self-contained command rather than a service.
- **Work the same everywhere developers work.** Behave identically across Linux, macOS, and Windows-via-WSL2, so a team and its agents get the same answers regardless of workstation.
- **Speak only OpenTelemetry.** Operate on standard OTLP signals — traces, logs, and metrics — and on no bespoke or proprietary telemetry format, so `otelq` fits any OpenTelemetry-instrumented codebase without lock-in.

## Non-Goals

- **A production observability platform.** `otelq` is a local-development and CI inspection tool. It is not for monitoring running production systems and makes no SLO, alerting, multi-tenant, or operational-scale promises.
- **A replacement for SigNoz, Grafana, Jaeger, or Tempo.** Those platforms remain the right tools for production-grade tracing, dashboards, and long-horizon analysis. `otelq` complements the inner loop; it does not compete with them.
- **Long-term telemetry storage.** `otelq` is concerned with recent, local telemetry. It is not a durable store, a data warehouse, or a retention system.
- **A metrics dashboard or visualization surface.** There is no UI and no charting. `otelq` answers queries; it does not render dashboards.
- **A daemon, web service, or always-on agent.** Remaining a command-line tool rather than a long-running process is a deliberate product constraint (see Constraints), not a temporary limitation. `otelq` does not aspire to become a server.
- **A telemetry generator or instrumentation library.** `otelq` reads what an app emits; it does not instrument the app or produce telemetry of its own.

## Target Users

| User group | What they need from `otelq` |
|------------|------------------------------|
| Backend developers | Confirm, while debugging locally, exactly which spans, logs, and metrics a code path emitted — without leaving the terminal or starting a backend |
| Frontend developers | Verify browser/client OpenTelemetry instrumentation just as easily as backend instrumentation, from the same tool |
| AI coding agents | Programmatically verify their own changes after a run — answer "what happened / what errored?" from telemetry, in JSON, in a single command |
| CI authors | Inspect the telemetry a failing or telemetry-asserting CI run produced, in a scriptable way, to diagnose without local reproduction |

## Success Metrics

Targets are qualitative; the point is the felt experience, not a dashboard number.

- **Seconds, not minutes, to first query.** The elapsed time from "the app emitted something" to "I can query it" is on the order of seconds, with no infrastructure spin-up in the path.
- **One command answers an agent's verification question.** An AI agent can answer a question like "what errored in the last ten minutes?" or "did the expected operation emit a span?" with a single `otelq` invocation returning structured output it can act on.
- **No standing processes.** Beyond a stock OpenTelemetry Collector container, there are zero long-lived processes required to capture and query local telemetry — nothing to keep running, watch, or tear down.
- **Verification becomes routine.** Because the cost of checking is near zero, developers and agents actually verify instrumentation as part of the inner loop rather than skipping it — the qualitative signal that the friction problem is solved.
- **Same answers on every platform.** A developer on macOS, a teammate on Linux, and a teammate on WSL2 get identical results from identical telemetry, so the tool is trusted as a shared source of truth.

## Constraints

- **CLI-only, for latency.** `otelq` must remain a command-line tool and must not become a daemon, server, or web service. This is a product constraint driven by speed: the primary consumers include AI agents in a tight act-then-verify loop, and a CLI keeps the LLM-to-tool round trip minimal and predictable. Introducing a service would add startup, connection, and lifecycle overhead that defeats the core promise of instant, infrastructure-free feedback.
- **OpenTelemetry-only.** `otelq` operates exclusively on standard OTLP signals (traces, logs, metrics). It must not introduce or depend on a bespoke telemetry format, so that it works with any OpenTelemetry-instrumented application and carries no proprietary lock-in.
- **Minimal dependencies.** The tool must stay lightweight and easy to run, favoring a small dependency footprint over breadth of features, so that adopting it is effortless and it remains fast to start.
- **Cross-platform parity.** `otelq` must behave identically on Linux, macOS, and Windows-via-WSL2. Platform-specific behavior would undermine its role as a trusted, shared verification tool across a team and its agents.
- **No persistent infrastructure.** Capturing and querying telemetry must not require provisioning or maintaining standing infrastructure beyond an ordinary OpenTelemetry Collector. `otelq` reads the telemetry that already exists locally; it does not own or operate a backend.

## Risks & Open Questions

- **Dependence on a Collector being configured to persist telemetry.** `otelq` reads telemetry a Collector has written locally. If a developer's environment is not set up to capture OTLP to a location `otelq` can read, the tool has nothing to query. The product bet is that this setup is one-time and lightweight; the risk is that environment setup friction undercuts the "zero-friction" promise for first-time users.
- **Recency-scoped by design.** `otelq` targets recent, local telemetry, not full history. Questions that genuinely need long-horizon data are out of scope and belong to a real observability platform; the open question is keeping that boundary legible so users do not mistake `otelq` for durable storage.
- **Agent output expectations.** Machine-readable output must stay stable and predictable enough for AI agents to depend on across versions. The risk is that evolving the output to serve humans better could destabilize what agents parse; balancing both audiences is an ongoing tension.
- **OpenTelemetry ecosystem drift.** OTLP and the surrounding tooling evolve. Staying "OpenTelemetry-only" means tracking that evolution; the risk is upstream changes to signal shapes or Collector behavior that `otelq` must follow to keep returning correct answers.
- **Local volume on long-running sessions.** A chatty app over a long local session can produce a large volume of telemetry. The product expectation is short, recent windows; managing the experience when the local corpus grows large is a known concern rather than a solved one.
