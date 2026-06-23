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
  - SPEC-otelq-incremental-cache
  - ADR-004-collector-in-docker-bind-mount
  - ADR-006-read-otlp-extension-quirks
  - CONTRACT-telemetry-directory
supersedes: null
superseded_by: null
ai_summary: "A per-minute parquet cache under telemetry/.otelq-cache/ accelerates recent/repeated queries while staying byte-identical to a full raw re-scan."
semantic_tags:
  - otelq
  - parquet
  - cache
  - duckdb
  - watermark
  - incremental
  - observability
---

# ADR-005 — Incremental Parquet Cache

## Context

`otelq` answers a query by parsing OTLP JSONL through the `duckdb-otlp` reader
([ADR-006](ADR-006-read-otlp-extension-quirks.md)). The reader cannot tail,
follow, or seek a file; every invocation re-parses whole files from byte zero.
For the dev workflow — repeated, narrow, recent-window queries against a
`telemetry/` corpus that only grows ([ADR-004](ADR-004-collector-in-docker-bind-mount.md))
— re-parsing the entire corpus on every command is wasteful and gets slower as
captured data accumulates.

The decision is whether, and how, to accelerate these repeated and recent
queries without changing which records a query returns. Any acceleration must be
a pure optimisation: a developer must be able to trust that a cached answer is
exactly the answer a full raw re-scan would have given.

This ADR records the architectural decision and its rationale only. The precise,
testable behaviour of the cache — sealing rules, watermark and margin
definitions, retention, routing, concurrency, crash recovery, and
cross-platform requirements — is the authoritative contract in
[SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md).

## Decision

Introduce a **per-minute Parquet cache** under `telemetry/.otelq-cache/` that
acts as a **transparent accelerator** for recent and repeated queries. Its
defining property is an **equivalence invariant**: for any command and time
range, a query answered from the cache returns results **byte-identical** to the
same query answered by a full raw re-scan. The cache changes performance, never
results.

The mechanics (rationale here; the binding contract is the SPEC) are:

- **Schema-versioned cursor.** A `cursor.json` tracks, per raw file, how many
  bytes have been consumed, plus a per-stream **event-time watermark** (the
  maximum record timestamp observed). It carries a schema version so an
  incompatible or unreadable cursor triggers a discard-and-rebuild rather than a
  failure.
- **Minute sealing past a margin.** Telemetry is partitioned by event-time
  minute. A minute is **sealed** into an immutable Parquet partition only once
  the watermark has advanced past the end of that minute by a configured
  **MARGIN**, so normal late/out-of-order arrival lands before the minute closes.
- **Event-time hot window, never wall-clock.** The **hot window** is the most
  recent **RETENTION** minutes measured against the observed event-time
  watermark — not the host's wall-clock. A query inside the hot window is served
  from sealed partitions plus the unsealed tail.
- **Stateless cold path for older ranges.** A query reaching data older than the
  hot window takes a **cold path**: a stateless raw scan of the requested range
  that reads and writes no cache or cursor state.
- **Dependency-free, cross-platform single-writer lock.** Sealing and eviction
  are serialised by an advisory lock built on `O_EXCL` sentinel-file creation —
  deliberately **not** `fcntl`, which is POSIX-only — so the same code path works
  on Linux, macOS, and Windows/WSL2. A run that loses the lock still answers its
  query; **readers never block** on the writer.
- **`--no-cache` bypass.** A `--no-cache` flag skips the cache entirely and forces
  the cold path, both as a debugging escape hatch and as the reference oracle for
  the equivalence invariant.

## Alternatives Considered

- **A persistent materialized DuckDB store with owned ingest bookkeeping.**
  Rejected as YAGNI for now. A full owned store (durable tables, schema
  migrations, its own ingest/compaction lifecycle) is far heavier than the dev
  use case justifies, and would make `otelq` the system of record for telemetry
  rather than a thin reader over the Collector's files. The per-minute Parquet
  cache delivers the needed speedup while keeping the raw `.jsonl` files the sole
  source of truth. This option is deferred, not foreclosed.
- **Tailing or watching the raw files to ingest incrementally in real time.**
  Rejected: the `duckdb-otlp` reader cannot follow or seek a file
  ([ADR-006](ADR-006-read-otlp-extension-quirks.md)), so there is no supported
  way to consume an append stream live. The cursor's byte-offset approach is the
  available substitute and runs per invocation, not continuously.

## Consequences

- The cache footprint is **bounded**: only ~RETENTION minutes of sealed
  partitions per signal are kept; older partitions are evicted. The cache cannot
  grow without bound the way the raw corpus does.
- The equivalence invariant is the cache's correctness contract and **must be
  guarded by a test** that diffs cached output against `--no-cache` output across
  commands and ranges. If that test ever diverges, the cache is wrong, by
  definition.
- The cache lives **inside** `telemetry/` (`telemetry/.otelq-cache/`), so it is
  scoped to the `CONTRACT-telemetry-directory` capture seam
  ([ADR-004](ADR-004-collector-in-docker-bind-mount.md)) and is cleared when that
  directory is reset.
- The full, testable behaviour — including rotation tolerance, idempotent
  sealing, crash recovery, and the unreliable-inode and stale-lock edge cases —
  is specified in
  [SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md), which
  this ADR governs and which must not contradict the equivalence invariant
  recorded here.
- Because sealing keys on event-time, `otelq` running on a host whose clock
  differs from the instrumented application still seals and queries the correct
  minutes.
