---
doc_type: adr
authoritative: false
stability: stable
status: superseded
decision_scope: architecture
audience:
  - ai
  - engineering
must_not_contain:
  - feature_requirements
  - implementation_walkthroughs
  - reversible_decisions
created: 2026-06-23
last_updated: 2026-07-07
related_documents:
  - ADR-003-duckdb-otlp-extension-pin-governance
  - ADR-004-collector-in-docker-bind-mount
  - ADR-005-incremental-parquet-cache
  - ADR-008-unified-cache-first-read-and-retention
  - ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0
  - SPEC-otelq-incremental-cache
supersedes: null
superseded_by: ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0
ai_summary: "Two centralized client-side mitigations for duckdb-otlp reader bugs: <=2000-row chunking around the 2048-row crash, and a timestamp 1000x correction."
semantic_tags:
  - otelq
  - duckdb
  - duckdb-otlp
  - read_otlp
  - timestamp
  - workaround
  - observability
---

# ADR-006 — read_otlp Extension Quirks

> **Superseded 2026-07-07 by
> [ADR-010](../adr/ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0.md).** Upstream
> duckdb-otlp v0.6.0 (DuckDB 1.5.4) fixes both defects mitigated here; the
> chunk-and-union pass, the oversized-batch skip, and the timestamp `REPLACE`
> are retired. Kept for the historical record.

## Context

`otelq` reads OTLP JSONL through the `duckdb-otlp` community extension
(smithclay), the same extension whose version pinning is governed by
[ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md). At the pinned
version, the extension's `read_otlp_*` table functions have two reproducible
defects that, left unmitigated, make the tool either crash or report nonsense:

1. **A row-count crash.** A single `read_otlp_*` call that returns more than
   DuckDB's `STANDARD_VECTOR_SIZE` (2048) rows corrupts memory and crashes the
   process. The extension writes the whole result into one output vector instead
   of yielding it in 2048-row chunks.
2. **A timestamp magnitude bug.** The extension stores nanosecond timestamps in a
   `TIMESTAMP_MS` column, so DuckDB interprets each value as **1000× too large**
   (a 2026 event reads as a year-58358 timestamp).

Both are upstream bugs in a third-party extension `otelq` does not own. The
decision is how to remain correct on top of a buggy reader without scattering
ad-hoc fixes across every query.

## Decision

Apply two **client-side mitigations**, **centralized** so the read path absorbs
the quirks and the rest of `otelq` (commands, cache, output) stays agnostic — it
sees only correct rows and correct timestamps.

**(a) The 2048-row crash — chunk and union.**
Before reading, slice each signal's JSONL into chunks of **≤ 2000 records**
(below the 2048 boundary, with margin), issue **one `read_otlp_*` call per
chunk**, then `UNION` the per-chunk results and **materialize** them into a
table. No single `read_otlp_*` call ever approaches the crash boundary.
Because one export batch is a single indivisible JSON object, a batch that
*alone* exceeds 2048 records cannot be chunked; such a batch is **skipped with a
warning** rather than crashing the run. (The practical remedy for that warning is
to lower the Collector's `send_batch_max_size`; see
[ADR-004](ADR-004-collector-in-docker-bind-mount.md).)

**(b) The timestamp bug — correct on read.**
Wrap every read in a `SELECT ... REPLACE` that rewrites the timestamp column as
`make_timestamp(epoch_us(timestamp) // 1000)`, dividing the misread value back
to the true instant. Every relation `otelq` builds — hot or cold — applies this
identical correction, so timestamps are right everywhere downstream.

Both mitigations are defined once in the read path and reused by every code path
that reads telemetry (the cold scan, the incremental cache's ingest, and the
plain connection helper), so the chunking limit and the timestamp fix can never
drift between paths.

## Alternatives Considered

- **A single large `read_otlp_*` call per file.** Rejected: this is exactly the
  trigger for defect (1) — any file yielding more than 2048 rows crashes the
  process. Chunking is not an optimisation, it is the avoidance of a hard crash.
- **Patching or forking the `duckdb-otlp` extension.** Rejected as out of scope.
  The extension is a pinned third-party dependency
  ([ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md)); maintaining a
  fork or a native build to fix upstream bugs is far heavier than two small
  client-side mitigations, and would entangle `otelq` in the extension's per-
  DuckDB-version build matrix.
- **Correcting timestamps ad hoc at each query site.** Rejected: scattering the
  `// 1000` fix across individual commands invites a missed site that silently
  reports year-58358 timestamps. Centralizing the correction in the read path
  removes that whole class of error.

## Consequences

- These are **workarounds for upstream bugs**, not intended permanent design.
  They are explicitly tied to the pinned extension version
  ([ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md)) and **should be
  revisited** when `duckdb-otlp` fixes the row-count crash and/or the
  `TIMESTAMP_MS` magnitude bug: at that point the chunking can relax and the
  timestamp `REPLACE` can be removed. The extension pin and these mitigations
  should be reviewed together.
- Chunking adds a slicing pass and multiple `read_otlp_*` calls per signal
  instead of one; this is the accepted cost of not crashing, and the materialized
  result keeps it invisible to callers.
- A single export batch larger than 2048 records is **not queryable** and is
  dropped with a warning; the upstream cause is too large a Collector batch, so
  the capture-side bound (`send_batch_max_size`, plus `rotation.max_megabytes`)
  in [ADR-004](ADR-004-collector-in-docker-bind-mount.md) keeps batches within
  range.
- Because both fixes live in the shared read path, the incremental cache
  ([ADR-005](../archive/ADR-005-incremental-parquet-cache.md), superseded by
  [ADR-008](ADR-008-unified-cache-first-read-and-retention.md)) inherits correct, fixed-
  timestamp rows for free, and its equivalence invariant (cached vs `--no-cache`)
  holds without the cache having to know anything about the extension's quirks.
