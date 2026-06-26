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
version: "1.0"
created: 2026-06-23
last_updated: 2026-06-23
related_documents:
  - ADR-004-collector-in-docker-bind-mount
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

- Default consumer-side location: the `telemetry/` directory under the current
  working directory (`<cwd>/telemetry/`); `--dir` overrides for other layouts.
- Default producer-side location: the container path `/telemetry`, bind-mounted
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

- `<signal>` is exactly one of `traces`, `logs`, `metrics`.
- `<timestamp>` is a producer-assigned rotation suffix that makes each backup
  filename unique within its signal.
- A signal's complete file set is the glob `<signal>*.jsonl` (the active file
  plus its rotated backups).

### Consumer-owned cache subtree

The `.otelq-cache/` subtree is **owned and managed exclusively by the consumer**.
The producer **must not** create, write, read, or delete anything inside it.

| Path | Owner | Description |
|------|-------|-------------|
| `.otelq-cache/cursor.json` | consumer | Cursor / state file |
| `.otelq-cache/<signal>/<minute>.parquet` | consumer | Per-signal sealed partitions |

The internal structure of `.otelq-cache/` is governed by
[SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md) and is
**not** part of this interface; only its location and consumer ownership are
contractual here.

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

- **Consumer read-only guarantee.** The consumer treats every `*.jsonl` file
  under the telemetry root as **READ-ONLY**: it never modifies, renames, or
  deletes any `*.jsonl` file. The consumer's only writes under the telemetry
  root are within `.otelq-cache/`.
- **Producer cache exclusion.** The producer **must not** write to, read from, or
  delete `.otelq-cache/` or any path beneath it.
- **Stable contract elements.** The following are fixed by this interface:
  - the active filenames `traces.jsonl`, `logs.jsonl`, `metrics.jsonl`;
  - the backup filename pattern `<signal>-<timestamp>.jsonl`;
  - the required `.jsonl` extension;
  - the per-line OTLP/JSON framing (one `Export<Signal>ServiceRequest` per line),
    UTF-8, uncompressed;
  - the file-to-reader signal mapping (including `metrics*.jsonl` → both gauge
    and sum readers);
  - the consumer-owned location `.otelq-cache/`.
- **Independent producers/consumers.** Any producer that writes files matching
  this layout and any consumer that reads them interoperate without further
  coordination; the directory is the sole point of agreement.

## Versioning

This is interface **version 1.0**.

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
