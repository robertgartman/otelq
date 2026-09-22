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
created: 2026-07-07
last_updated: 2026-07-07
related_documents:
  - ADR-003-duckdb-otlp-extension-pin-governance
  - ADR-006-read-otlp-extension-quirks
  - ADR-008-unified-cache-first-read-and-retention
  - SPEC-otelq-cli
  - SPEC-otelq-incremental-cache
supersedes: ADR-006-read-otlp-extension-quirks
superseded_by: null
retrieval_priority: high
ai_summary: "Bump the pin to DuckDB 1.5.4 + otlp v0.6.0 (crash and timestamp bugs fixed upstream); adopt the extension's reader schema natively as otelq's internal data model; keep the tail-sanitization seam."
semantic_tags:
  - otelq
  - duckdb
  - duckdb-otlp
  - version-pin
  - breaking-schema
  - native-schema
  - performance
  - observability
---

# ADR-010 — Adopt DuckDB 1.5.4 + duckdb-otlp v0.6.0

## Context

otelq's read path was shaped by two upstream defects in the `duckdb-otlp`
extension at the previous pin (DuckDB 1.5.3 / otlp `e0f079f`), mitigated by
[ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md): a memory-corrupting
crash on any single `read_otlp_*` call yielding more than 2048 rows (forcing
≤2000-record chunk files unioned per signal) and nanosecond timestamps stored in
a `TIMESTAMP_MS` column (forcing a ÷1000 `REPLACE` on every read). The chunked
UNION dominates indexing cost: DuckDB executes the per-chunk table-function arms
serially, so a ~400 MB corpus takes ~75 s to index while an unchunked read of
the same data takes ~1 s.

Upstream **duckdb-otlp v0.6.0**, built for **DuckDB 1.5.4** and published to the
community-extensions repository (extension version `5f32698`), fixes both
defects — the scan now yields output in ≤`STANDARD_VECTOR_SIZE` slices (a
300 000-row single-call read was verified crash-free) and timestamps are native
`TIMESTAMP_NS`. It is, however, a **breaking schema change**: every reader
column is renamed or retyped (e.g. `timestamp` → `time_unix_nano`,
`span_name` → `name`, `duration` → `duration_time_unix_nano`,
`metric_name` → `name`; gauge/sum values split into `int_value`/`double_value`;
histogram `bucket_counts`/`explicit_bounds` become typed lists), and its error
tolerance changed: a partial trailing line or non-OTLP garbage line now raises a
hard IO error where the old version skipped it. Empirical probing also
established: glob paths are supported but list-of-file arguments are not; files
are subject to a 100 MB per-file reader cap; a cross-signal read (e.g. the
traces reader over a logs file) still returns zero rows with the correct schema.

The decision is whether and how to adopt this release.

## Decision

**Bump the pin to `duckdb==1.5.4` / otlp v0.6.0**, executed through the
mandatory pin-bump checklist of
[ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md), and **adopt the
extension's reader schema natively as otelq's internal data model**:

1. **Native schema adoption.** The six reader relations (`traces`, `logs`,
   `metrics_gauge`, `metrics_sum`, `metrics_histogram`,
   `metrics_exp_histogram`) carry the v0.6.0 reader columns **verbatim** —
   `SELECT *` at the read seam, no renames, no retypes, no unit conversions.
   The upstream duckdb-otlp project documentation is thereby the reference for
   otelq's stored data model; there is no parallel otelq column dictionary to
   maintain, and no lossy conversion (ns-exact `TIMESTAMP_NS` event-times,
   ns-exact `duration_time_unix_nano`, split `int_value`/`double_value`, typed
   list buckets are all preserved). Consequences for otelq's own constructs:
   - The otelq-defined `metrics` union view uses upstream-style column names
     (`time_unix_nano`, `service_name`, `name`, `metric_type`, `value`,
     `unit`), where `value` coalesces gauge/sum `double_value`/`int_value` and
     surfaces histogram/exp_histogram `sum`.
   - Generic cache logic (minute sealing, hot-window filtering, watermarks)
     keys on a per-signal event-time column: `start_time_unix_nano` for
     traces, `time_unix_nano` for all other signals.
   - Built-in command **output** columns remain presentation-friendly aliases
     (`timestamp`, `duration_ms`, …) computed in the command SQL — a report
     format, not a data model.
2. **Retire the ADR-006 mitigations.** The ≤2000-row chunk-and-union pass, the
   oversized-batch skip, and the ÷1000 timestamp `REPLACE` are removed; each
   signal is read in one call (per ≤100 MB sanitized file). This ADR
   **supersedes** [ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md).
3. **Keep the tail-sanitization seam.** Because v0.6.0 hard-errors on partial
   or garbage lines, the existing Python pass that filters undecodable lines
   before handing bytes to the reader is retained — it is now the *only* thing
   standing between a half-written Collector line and a failed query. Sanitized
   content is written to temp files split below the 100 MB reader cap.
4. **Invalidate the cache by version.** The cached parquet content derived from
   the old reader (renamed columns, millisecond-truncated timestamps) is
   incompatible with the native v0.6.0 shape; the cache schema version is
   bumped so existing caches self-wipe and rebuild
   (SPEC-otelq-incremental-cache FR-14) instead of mixing schemas.

## Alternatives Considered

- **Stay on 1.5.3 and parallelize the chunked UNION client-side.** A working
  thread-pool prototype achieved ~4.5× on the staging pass, but it optimizes a
  workaround for a bug that upstream has now fixed outright (~70× on the same
  pass), adds concurrency machinery to keep forever, and leaves the timestamp
  bug and the 2048 crash latent. Rejected in favor of removing the root cause.
- **Project the v0.6.0 schema back to otelq's pre-existing canonical columns
  at the read boundary.** Rejected: it preserves the old `sql` surface, but at
  the cost of a permanent otelq-maintained column dictionary that shadows
  upstream's, lossy conversions (ns→ms duration truncation, collapsed
  `int_value`/`double_value`, stringified bucket lists, dropped `event_name`
  and log `flags`), and a projection layer every future extension bump must be
  reconciled against. Adopting the schema natively makes the upstream project
  the single reference for the stored data model.
- **Support both schemas behind a runtime switch.** Rejected: ADR-003's exact
  pin means exactly one extension version is ever loaded; dual-schema code is
  dead weight and doubles the test matrix.
- **Drop the Python sanitization pass and let the reader's errors surface.**
  Rejected: a half-written trailing line is a *normal* state of a live
  Collector file, not an error; failing the query on it violates the
  fail-friendly rule and SPEC-otelq-cli FR-21.

## Consequences

- **Indexing cost drops dramatically** (chunked serial UNION → one read per
  signal); the ~75 s cold index of a ~400 MB corpus becomes seconds. Warm-path
  behavior is unchanged.
- **The oversized-batch limitation disappears.** Export batches of any size are
  readable; the skip-with-warning behavior (old SPEC-otelq-cli FR-20/EC-8,
  SPEC-otelq-incremental-cache FR-15/EC-5) is obsolete and those SPECs are
  revised accordingly. The Collector `send_batch_max_size` bound in
  [ADR-004](ADR-004-collector-in-docker-bind-mount.md) is no longer
  load-bearing for otelq correctness.
- **The `sql` surface changes.** Stored relations expose the upstream column
  names and types; queries written against the old canonical columns
  (`timestamp`, `span_name`, `duration`, `metric_name`) must be rewritten. The
  `sql` cheat-sheet in [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md) FR-2 is the
  authoritative user-facing list; the upstream duckdb-otlp documentation is
  the authoritative semantic reference.
- **Timestamps are exact to the nanosecond** end to end (`TIMESTAMP_NS`
  storage, ns-exact durations); the presented precision of command output
  remains as specified in [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md) FR-16.
  The far-future clamp remains (it guards producer clock skew, not the
  extension bug).
- **One-time cache rebuild** on first run after upgrade (version-triggered
  self-wipe); no user action needed.
- **The sanitization seam is now correctness-critical** rather than
  belt-and-braces, and carries the 100 MB-per-file split responsibility; its
  behavior is specified in SPEC-otelq-incremental-cache FR-15 and
  SPEC-otelq-cli FR-21.
- **A future upstream schema change is a schema change for otelq** — it flows
  through to the relations, the cache (version bump + self-wipe), and the
  `sql` surface, and is evaluated as part of the ADR-003 pin-bump checklist.
  The pin remains governed by
  [ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md) — this bump does
  not loosen it.
