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
last_updated: 2026-07-02
related_documents:
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-005-incremental-parquet-cache
  - ADR-006-read-otlp-extension-quirks
  - ADR-008-unified-cache-first-read-and-retention
  - SPEC-otelq-incremental-cache
  - CONTRACT-telemetry-directory
supersedes: null
superseded_by: null
ai_summary: "Stock pinned otel/...-contrib Collector as an opt-in Compose service writing three .jsonl files to a bind-mounted telemetry dir."
semantic_tags:
  - otelq
  - opentelemetry
  - collector
  - docker
  - compose
  - fileexporter
  - jsonl
  - observability
---

# ADR-004 — Collector in Docker via Bind Mount

## Context

`otelq` queries OTLP telemetry that some component must first capture and write to
disk. The host CLI reads bind-mounted files only and never opens a network socket
or talks to an SDK (the host/container split is recorded in
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md)). That makes the capture
component an OpenTelemetry Collector running in a container, exporting OTLP it
receives to files on a shared host directory.

The constraints that force the shape of this Collector are not free design
choices — they are dictated by the downstream reader (`duckdb-otlp`, the
smithclay OTLP community extension; see
[ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md) and
[ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md)) and by the need to keep a
dev-only collector cheap, off-by-default, and observable without any extra
tooling.

This ADR records the architectural decision and the load-bearing constraints of
that capture seam. The persistent file layout it produces is the stable
interface other components depend on, so it is governed as
`CONTRACT-telemetry-directory`.

## Decision

Run a **stock** `otel/opentelemetry-collector-contrib` image — pinned to tag
**`0.119.0`** — as an **opt-in** Docker Compose service guarded by the Compose
profile **`otel`**. The service is not started by a default `compose up`; it is
brought up only when telemetry capture is wanted.

The Collector bind-mounts two host paths:

1. its configuration file, mounted **read-only**, and
2. the host `.telemetry/` directory, mounted read-write, into which it writes.

Its pipelines use three `file` exporters, one per signal, each writing
`/.telemetry/<signal>.jsonl` (`traces.jsonl`, `logs.jsonl`, `metrics.jsonl`).
The OTLP receiver listens on the standard gRPC `4317` and HTTP `4318` ports; an
instrumented application captures telemetry by pointing its
`OTEL_EXPORTER_OTLP_ENDPOINT` at `localhost:4317` (gRPC) or `localhost:4318`
(HTTP).

The following constraints are **load-bearing** and must not be changed casually:

- **File extension must be `.jsonl`.** `duckdb-otlp` selects its parse path by
  file extension. A file written as `.json` is silently treated as a different
  format and yields **no rows** with no error — a failure mode that looks like
  "no telemetry" rather than a misconfiguration. The exporter `path` therefore
  must end in `.jsonl`.
- **`rotation.max_megabytes: 50` is a hard safety bound, not a tuning knob.**
  `duckdb-otlp` cannot read a single file larger than ~100 MB. Capping each
  active file at 50 MB keeps every individual `.jsonl` (active file and rotated
  backups alike) comfortably under that read ceiling, and keeps a full
  per-query re-scan of the corpus viable. The incremental cache
  ([ADR-005](../archive/ADR-005-incremental-parquet-cache.md), superseded by
  [ADR-008](ADR-008-unified-cache-first-read-and-retention.md)) further relies on rotation
  being size-triggered and offset-stable.
- **`memory_limiter` must be the first processor in every pipeline.** The
  Collector's own guidance requires the memory limiter to run ahead of any
  batching so back-pressure is applied before memory is committed. Placing it
  anywhere but first defeats its purpose. A `batch` processor follows it.
- **A `debug` exporter belongs in every pipeline.** It is a free, always-on
  probe: `docker logs` on the Collector container immediately shows whether a
  signal is flowing, with zero additional infrastructure. It is the first thing
  to check when `otelq` reports no data.

Stack-specific noise filtering is **not** part of this decision. An adopter
whose stack emits high-volume, low-value spans (for example, ORM
connection-pool spans) may add an optional Collector `filter` processor to drop
them; that is a per-adopter tuning choice, deliberately left out of the stock
configuration so the default capture path stays faithful to what the
application emitted.

## Alternatives Considered

- **Build a custom OpenTelemetry Collector (OCB) image.** Rejected: there is
  nothing to build. The `-contrib` image already bundles the OTLP receiver and
  the `file`/`debug`/`filter`/`batch`/`memory_limiter` components this seam
  uses, so a custom OCB distribution adds a build step and maintenance burden
  with zero runtime benefit. The default overhead of the contrib image in a
  dev/CI context is negligible.
- **An always-on Collector.** Rejected: capture is a debugging aid, not a
  baseline dependency. Gating it behind the `otel` Compose profile keeps it off
  by default so a normal `compose up` neither runs the container nor writes to
  `.telemetry/`; it is opted into only when needed.
- **A non-stock, locally patched image to work around reader quirks.** Rejected:
  the `duckdb-otlp` quirks are mitigated **client-side** in `otelq`
  ([ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md)), leaving the Collector
  image stock and replaceable.

## Consequences

- The on-disk shape this produces — the directory location, the three
  per-signal `.jsonl` filenames, the `.jsonl` extension requirement, and the
  size-rotation behaviour — is the stable interface that `otelq` and its cache
  read against. That shape is governed as **`CONTRACT-telemetry-directory`**;
  changes to it are contract changes, not Collector tuning.
- Capturing telemetry from any application is a configuration-only step: start
  the `otel`-profile service and point the application's
  `OTEL_EXPORTER_OTLP_ENDPOINT` at `localhost:4317`/`4318`. No code change in
  either the application or `otelq` is required.
- Pinning the contrib image tag (`0.119.0`) keeps the exporter's file format and
  rotation semantics reproducible across machines and CI. The pin should be
  reviewed together with the `duckdb-otlp` extension pin
  ([ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md)) so reader and
  writer stay format-compatible.
- The `debug` exporter means a first-line "is anything flowing?" check costs
  nothing and needs no extra tooling; the corollary is a small, constant amount
  of Collector log output while capture is running.
- Because the config is mounted read-only, the running Collector cannot mutate
  its own configuration, and the host file remains the single source of truth
  for the capture pipeline.
