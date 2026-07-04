---
doc_type: contract
authoritative: true
stability: stable
status: active
decision_scope: interface
audience:
  - ai
  - engineering
must_not_contain:
  - business_context
  - behavioral_explanations
  - decision_rationale
version: "1.1"
created: 2026-06-23
last_updated: 2026-07-04
related_documents:
  - ADR-004-collector-in-docker-bind-mount
  - ADR-009-query-history-triage-store
  - SPEC-otelq-cli
  - SPEC-otelq-incremental-cache
---

# CONTRACT — Telemetry Directory

## Purpose

Define the stable on-disk interface between the dev OpenTelemetry Collector (the
**producer**) and the `otelq` CLI (the **consumer**). The shared bind-mounted
directory and its JSONL file layout are the entire integration surface; there is
no network coupling between the two sides.

## Interface / Schema Definition

### Telemetry root

The producer and consumer agree on a single **telemetry root** directory:

- Default consumer-side location: the `.telemetry/` directory under the current
  working directory (`<cwd>/.telemetry/`); `--dir` overrides for other layouts.
- Default producer-side location: the container path `/.telemetry`, bind-mounted
  to the same host directory.

All paths below are relative to the telemetry root.

### Directory layout

| Path | Producer | Owner | Description |
|------|----------|-------|-------------|
| `traces.jsonl` | writes | producer | Active trace stream |
| `logs.jsonl` | writes | producer | Active log stream |
| `metrics.jsonl` | writes | producer | Active metric stream |
| `<signal>-<timestamp>.jsonl` | writes | producer | Size-rotated backups of a signal's active file |
| `.otelq-cache/` | must not write | consumer | Consumer-owned cache (see below) |
| `.otelq-history/` | must not write | consumer | Consumer-owned query-history store (see below) |

- `<signal>` is exactly one of `traces`, `logs`, `metrics`.
- `<timestamp>` is a producer-assigned rotation suffix that makes each backup
  filename unique within its signal.
- A signal's complete file set is the glob `<signal>*.jsonl` (the active file
  plus its rotated backups).

### Consumer-owned subtrees

The subtrees named with the `.otelq-` prefix — currently `.otelq-cache/` and
`.otelq-history/` (v1.1) — are **owned and managed exclusively by the
consumer**. The producer **must not** create, write, read, or delete anything
inside them.

| Path | Owner | Description |
|------|-------|-------------|
| `.otelq-cache/cursor.json` | consumer | Cursor / state file |
| `.otelq-cache/<signal>/<minute>.parquet` | consumer | Per-signal sealed partitions |
| `.otelq-history/` | consumer | Query-history store (journal + Parquet tables + audit) |

The internal structure of `.otelq-cache/` is governed by
[SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md); the
internal structure of `.otelq-history/` is governed by
[ADR-009](../adr/ADR-009-query-history-triage-store.md) and its implementation.
Neither internal structure is part of this interface; only their locations and
consumer ownership are contractual here. Files inside consumer-owned subtrees
are **not** part of the parent root's signal set regardless of extension (the
signal globs are non-recursive).

### File format

- Encoding: **OTLP/JSON**, UTF-8.
- Framing: **JSONL** — one OTLP `Export<Signal>ServiceRequest` object **per
  line**, terminated by `\n`. A line is one complete export batch.
- Extension: the **`.jsonl` extension is REQUIRED**. The consumer selects its
  reader by file extension; a telemetry file without the `.jsonl` extension is
  outside this contract and will not be read.
- Compression: **none**. Files are written and read as plain text.

### Signal set and file-to-reader mapping

There are three raw byte-streams (`traces`, `logs`, `metrics`) and four consumer
signals. The single `metrics.jsonl` stream is consumed by two readers.

| File (glob) | Top-level OTLP object | Consumer reader(s) |
|-------------|-----------------------|--------------------|
| `traces*.jsonl` | `ExportTraceServiceRequest` (`resourceSpans`) | `read_otlp_traces` |
| `logs*.jsonl` | `ExportLogsServiceRequest` (`resourceLogs`) | `read_otlp_logs` |
| `metrics*.jsonl` | `ExportMetricsServiceRequest` (`resourceMetrics`) | `read_otlp_metrics_gauge` **and** `read_otlp_metrics_sum` |

`metrics.jsonl` is one stream read by two readers (gauge and sum); both consume
the same lines.

### Rotation (producer side)

The producer rotates each active file by size:

| Parameter | Value |
|-----------|-------|
| `max_megabytes` (rotation threshold per active file) | `50` |
| `max_backups` (retained backups per signal) | `5` |

- On rotation the active file is renamed to a `<signal>-<timestamp>.jsonl`
  backup and a new active `<signal>.jsonl` is created.
- Each individual file (active or backup) stays under the consumer reader's
  **100 MB per-file ceiling**; the `50 MB` threshold leaves headroom for the
  trailing batch flushed at rotation.
- A reset of an active file is performed as a **truncation-in-place**: the active
  `<signal>.jsonl` **retains its inode** across the reset (the file is not
  unlinked and recreated).

## Field Semantics

- `traces.jsonl`, `logs.jsonl`, `metrics.jsonl` — the current (active) file for
  each signal; appended to by the producer until rotated or reset.
- `<signal>-<timestamp>.jsonl` — an immutable, already-rotated segment of that
  signal; subject to deletion by the producer once `max_backups` is exceeded.
- One line = one OTLP `Export<Signal>ServiceRequest`; the byte offset of a line
  within its file is stable and meaningful (the consumer addresses content by
  `(file identity, byte offset)`).

## Compatibility Guarantees

- **Consumer read-only guarantee.** The consumer treats every producer-owned
  `*.jsonl` file under the telemetry root as **READ-ONLY**: it never modifies,
  renames, or deletes any of the producer's files. The consumer's only writes
  under the telemetry root are within the consumer-owned subtrees
  (`.otelq-cache/`, `.otelq-history/`).
- **Producer subtree exclusion.** The producer **must not** write to, read
  from, or delete any consumer-owned subtree (`.otelq-cache/`,
  `.otelq-history/`) or any path beneath them.
- **Stable contract elements.** The following are fixed by this interface:
  - the active filenames `traces.jsonl`, `logs.jsonl`, `metrics.jsonl`;
  - the backup filename pattern `<signal>-<timestamp>.jsonl`;
  - the required `.jsonl` extension;
  - the per-line OTLP/JSON framing (one `Export<Signal>ServiceRequest` per line),
    UTF-8, uncompressed;
  - the file-to-reader signal mapping (including `metrics*.jsonl` → both gauge
    and sum readers);
  - the consumer-owned locations `.otelq-cache/` and `.otelq-history/`.
- **Independent producers/consumers.** Any producer that writes files matching
  this layout and any consumer that reads them interoperate without further
  coordination; the directory is the sole point of agreement.

## Versioning

This is interface **version 1.1**.

**v1.1 (additive, backward-compatible).** Adds the consumer-owned
`.otelq-history/` subtree (see
[ADR-009](../adr/ADR-009-query-history-triage-store.md)) and groups the
consumer-owned locations under one clause. No existing filename, extension,
framing, mapping, or ownership rule changed; v1.0 producers and consumers
remain fully compatible.

A change to any of the following is **breaking** and **requires a major version
bump** of this contract:

- file names (active or backup pattern);
- the required `.jsonl` extension or the extension-based selection rule;
- the file-to-reader signal mapping;
- the per-line OTLP/JSON framing (one object per line), encoding, or the
  no-compression rule.

Additive, backward-compatible changes (e.g. introducing a new signal file
alongside the existing ones) **may** be made with a minor version bump, provided
existing files, extensions, framing, and mappings are unchanged.
