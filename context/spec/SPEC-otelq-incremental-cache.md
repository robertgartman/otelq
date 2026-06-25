---
doc_type: spec
authoritative: true
stability: evolving
status: active
decision_scope: feature
audience:
  - ai
  - engineering
must_not_contain:
  - product_vision
  - architectural_rationale
  - api_schema_definitions
created: 2026-06-22
last_updated: 2026-06-23
related_documents:
  - ADR-005-incremental-parquet-cache
  - ADR-004-collector-in-docker-bind-mount
  - SPEC-otelq-cli
ai_summary: "otelq's incremental parquet cache: per-minute sealing, retention eviction, hot/cold read routing, results identical to a full raw re-scan."
semantic_tags:
  - otelq
  - telemetry
  - parquet
  - cache
  - duckdb
  - observability
---

# SPEC — otelq Incremental Parquet Cache

## Purpose

Define the exact behavior of an incremental, persistent cache for the `otelq`
dev-telemetry CLI (`otelq.py`), so that repeated and recent queries
read a small delta of newly-written telemetry instead of re-parsing the entire
`telemetry/*.jsonl` corpus on every invocation — while returning results
identical to the current full-scan tool.

Architectural context (the dev Collector file-export seam, and why a cache is
introduced) is recorded in [ADR-005](../adr/ADR-005-incremental-parquet-cache.md)
(cache design) and [ADR-004](../adr/ADR-004-collector-in-docker-bind-mount.md)
(the Collector file-export seam), in
`context/adr/ADR-005-incremental-parquet-cache` and
`context/adr/ADR-004-collector-in-docker-bind-mount`.
This document specifies *what the cache must do*, not why the approach was chosen.

## Scope

**Covered:** how otelq persists telemetry to a parquet cache, when it seals and
evicts cached data, how it routes a query between the cache and the raw files,
how it stays correct under rotation, concurrency, crashes, and version changes,
and how it behaves identically across supported platforms.

**Not covered:** the OTel Collector configuration and rotation settings (an input,
not changed here); the `duckdb-otlp` reader extension; the set of signals, columns,
and views exposed to queries (unchanged from the current tool); and the design
rationale for the cache (see [ADR-005](../adr/ADR-005-incremental-parquet-cache.md)).

### Definitions

- **Raw files** — the Collector's append-only, size-rotated JSONL outputs
  (`telemetry/<signal>.jsonl` active file + `telemetry/<signal>-<ts>.jsonl`
  rotated backups). otelq treats these as read-only inputs.
- **Signal** — one of `traces`, `logs`, `metrics_gauge`, `metrics_sum`.
- **Event-time** — a record's own timestamp (`timeUnixNano`), never wall-clock.
- **Minute M** — the UTC clock-minute `[M, M+1)`, keyed filename-safe as
  `<date>T<hour>-<minute>` (e.g. `2026-06-22T10-30`; colon-free).
- **Cache** — `telemetry/.otelq-cache/`, containing per-signal sealed parquet
  partitions `<signal>/<minute>.parquet` and a `cursor.json` state file.
- **RETENTION** — the cache's rolling window; default **30 minutes**.
- **MARGIN** — the watermark lateness allowance; default **2 minutes**.
- **Hot window** — the most recent `RETENTION` minutes of event-time.
- **Watermark (per signal)** — the maximum event-time otelq has observed for that
  signal across all ingest so far.
- **Hot path** — cursor-driven incremental ingest that seals parquet and answers
  queries from cache plus the unsealed tail.
- **Cold path** — a stateless scan of the raw files for an explicitly requested
  time range, using no cache or cursor state.

## Functional Requirements

- **FR-1 — Cache store layout.** otelq **must** persist sealed telemetry as
  `telemetry/.otelq-cache/<signal>/<minute>.parquet` for each signal, alongside a
  single `telemetry/.otelq-cache/cursor.json` state file.
- **FR-2 — Incremental ingest.** On each run the hot path **must** read only the
  raw bytes not yet recorded as consumed by the cursor, and **must not** re-parse
  raw data already consumed in a previous run.
- **FR-3 — Cursor and rotation tolerance.** The cursor **must** identify each raw
  file by `(inode, first-256-byte fingerprint, size)` mapped to a
  `bytes_consumed` offset. A size-triggered rotation that renames the active file
  to a backup **must** cause neither re-parsing of already-consumed bytes nor loss
  of not-yet-consumed bytes.
- **FR-4 — Per-signal watermark sealing.** A minute `M` **must** be sealed for a
  signal only once that signal's watermark has reached `M + 1 minute + MARGIN`
  (i.e. all of minute `M` lies at least `MARGIN` behind the latest observed
  event-time). Signals **must** seal independently of one another.
- **FR-5 — Complete, retention-bounded sealing.** A sealed parquet partition
  **must** contain every record for that minute consumed up to the moment it
  sealed (sealing only once the watermark has passed the minute's end by `MARGIN`,
  so it is complete under normal arrival ordering). A record arriving for an
  already-sealed minute (late/out-of-order beyond `MARGIN`) **must** be retained in
  the pending tail rather than added to the immutable partition, so the hot read
  still returns it (FR-11). otelq **must** seal only minutes within the hot window;
  older minutes **must not** be sealed even when their raw bytes are consumed to
  advance the cursor.
- **FR-6 — Retention and eviction.** otelq **must** remove parquet partitions
  whose minute is older than the hot window, so the cache footprint stays bounded
  to approximately `RETENTION` minutes per signal.
- **FR-7 — Hot-path read.** A query whose required time range lies within the hot
  window **must** be answered from the union of sealed parquet partitions and the
  current run's unsealed tail records, without scanning rotated raw backups.
- **FR-8 — Cold-path fallback.** A query that explicitly requires data older than
  the hot window **must** be answered by scanning the raw files directly for the
  requested range. The cold path **must not** read or write cursor or cache state.
- **FR-9 — Recent-by-default query scope.** Commands without an explicit time
  window (`summary`, `slow`, `logs`, `metric`, `sql`) **must** default to the hot
  window. A new `--all` global flag (and an explicit `--since` beyond the hot
  window) **must** widen the query to the full raw history via the cold path.
- **FR-10 — Lookup routing.** `trace <id>` and `metric <name>` **must** query the
  hot cache first and **must** fall back to the cold path when the result is not
  found (trace) or to widen coverage when explicitly requested.
- **FR-11 — Result equivalence.** For any command and time range, results produced
  with the cache **must** be identical to results produced by a full raw re-scan
  of the same range. The cache is an accelerator only; it **must not** change which
  records a query returns.
- **FR-12 — Single-writer concurrency.** Sealing, eviction, and cursor writes
  **must** be serialized by an advisory lock implemented without `fcntl` (e.g. an
  `O_EXCL` sentinel file with a stale-lock reaper). A run that cannot acquire the
  lock **must** still answer its query (from existing parquet plus a raw tail
  scan) and **must** skip sealing rather than block or fail. Readers **must not**
  block on the writer.
- **FR-13 — Atomic, cross-platform file operations.** All cache writes **must** be
  performed as a temporary file followed by `os.replace` (never `os.rename` onto an
  existing path). Raw files **must** be read in binary mode so byte offsets are
  exact on every platform (never text-mode `tell()`). Minute keys **must** be UTC
  and contain no `:`; all paths **must** use `pathlib`. The implementation **must**
  not depend on any POSIX-only primitive. Supported platforms are Linux, macOS,
  and Windows-via-WSL2 natively; native Windows is supported subject to the
  pre-existing requirement that the `duckdb-otlp` extension ships a build for the
  pinned DuckDB version.
- **FR-14 — Versioned, self-healing cache.** `cursor.json` **must** carry a schema
  version. On a version mismatch, or when cache state is missing or unreadable,
  otelq **must** discard and rebuild the cache rather than fail. Leftover `*.tmp`
  files **must** be ignored when reading and removed when stale.
- **FR-15 — Robust tail parsing.** The incremental reader **must** preserve the
  existing handling of a partially-written trailing JSON line (skip on decode
  error) and of a single export batch exceeding the reader's 2048-row limit (skip
  with a warning).
- **FR-16 — `otel-clean` wipes the cache.** Clearing captured telemetry **must**
  also remove `telemetry/.otelq-cache/`, so a clean is a full reset.
- **FR-17 — `--no-cache` bypass.** A `--no-cache` global flag **must** force a pure
  cold (raw-only) scan that neither reads nor writes the cache, for debugging and
  for equivalence verification.

## Edge Cases & Failure Modes

- **EC-1 — Rotation mid-minute.** A minute whose records are split across the
  active file and a freshly-rotated backup is still sealed as one complete minute.
- **EC-2 — Idle then run.** After a long gap with no otelq invocation, a single
  run performs a larger catch-up parse to advance the cursor; results are correct
  and only hot-window minutes are sealed.
- **EC-3 — Concurrent runs.** Two runs that would seal the same minute do not
  corrupt or duplicate the partition; sealing is idempotent via atomic replace.
- **EC-4 — Partial trailing line.** A half-written final JSONL line is skipped and
  the run still succeeds.
- **EC-5 — Oversized batch.** A single export batch exceeding 2048 records is
  skipped with a warning, as today.
- **EC-6 — Unreliable inode.** On filesystems where `st_ino` is `0` or
  non-unique (e.g. FAT32, some network shares), or after inode reuse, the
  fingerprint and size disambiguate; the worst outcome is a safe re-read or a
  cold-path answer, never incorrect data.
- **EC-7 — Cache version mismatch or corruption.** A `cursor.json` with an
  unknown version or unreadable content triggers a self-wipe and rebuild; the
  query still returns correct results.
- **EC-8 — Stale temp file.** A `*.tmp` left by a crashed write is never served
  and is reaped.
- **EC-9 — Empty or cold cache.** The first run (or the first run after
  `otel-clean`) answers correctly via the cold path and begins populating the
  cache.
- **EC-10 — Backup evicted by the Collector.** If the Collector's `max_backups`
  deletes a raw backup before otelq consumes it, the cache holds nothing the raw
  files dropped and otelq does not crash; the reported range reflects available
  raw data.
- **EC-11 — Crash between seal and cursor advance.** If a run dies after sealing a
  minute but before persisting the advanced cursor, the next run re-derives state
  safely: sealed partitions stay immutable (idempotent skip) and no record is
  lost. A crash in the narrow window mid-ingest may transiently leave the most
  recent unsealed minutes over-counted in the cache; a `--no-cache` query is always
  exact and `otel-clean` clears the cache. In normal, crash-free operation the hot
  read is exactly equal to a full raw re-scan (FR-11), including out-of-order and
  byte-identical duplicate records.
- **EC-12 — Divergent clocks.** Sealing and the watermark are driven by record
  event-time, so otelq running on a host whose wall-clock differs from the
  applications' still seals the correct minutes.

## Acceptance Criteria

> Given/When/Then, each independently testable. New cache behavior is exercised
> with **file-based** fixtures — fabricated OTLP JSONL written to a temporary
> `telemetry` directory — in contrast to the existing in-memory `synth_conn`
> fixture, because the cache operates on files. Hints reference `just otelq`,
> `just otelq-test`, and `tests/test_otelq.py`.

- **AC-1** (Verifies FR-1, FR-5): Given fabricated traces spanning three complete
  minutes within the hot window, when otelq runs once, then
  `telemetry/.otelq-cache/traces/<minute>.parquet` exists for each sealed minute
  and each file contains exactly that minute's records.
  *Verification hint: run a command via the cache, then list the cache dir and
  read each parquet with DuckDB `read_parquet`.*
- **AC-2** (Verifies FR-2): Given a populated cache, when otelq runs again with no
  new raw bytes appended, then it parses zero raw records on the hot path (only
  the unsealed tail, if any).
  *Verification hint: instrument the ingest reader's consumed-byte count in a unit
  test and assert it equals 0 for an unchanged corpus.*
- **AC-3** (Verifies FR-3, EC-1): Given an active raw file consumed up to offset
  X, when it is renamed to a backup and a new active file is created (size
  rotation) and more records are appended, then the next run reads the unread
  tail of the rotated backup and the new active file exactly once each, with no
  gap and no duplication.
  *Verification hint: simulate lumberjack rename in a temp dir; assert sealed row
  counts equal the total appended.*
- **AC-4** (Verifies FR-4, INV-3, EC-12): Given records whose latest event-time is
  `T`, when otelq seals, then no minute `M` with `M + 1min + MARGIN > T` is sealed,
  and minutes fully behind that boundary are sealed — using event-time regardless
  of the host wall-clock.
  *Verification hint: fabricate records ending at a known event-time; assert the
  set of sealed minute keys.*
- **AC-5** (Verifies FR-5, EC-2): Given a cold start over raw data spanning many
  hours, when otelq runs once, then only minutes inside the hot window are sealed
  to parquet and older consumed minutes are not written.
  *Verification hint: fabricate 3h of data; assert sealed partition count ≈
  RETENTION, not the full span.*
- **AC-6** (Verifies FR-6): Given a cache containing partitions older than the hot
  window, when otelq runs, then those partitions are removed and only ~RETENTION
  minutes per signal remain.
  *Verification hint: pre-seed old parquet files; run; assert they are gone.*
- **AC-7** (Verifies FR-7): Given a query within the hot window, when it executes,
  then the result is built from sealed parquet plus the unsealed tail, and no rows
  are re-parsed from a fully-consumed rotated backup into the query relations (the
  incremental cursor yields an empty delta for it; the cursor may still stat and
  read a backup's prefix bytes to confirm its identity).
  *Verification hint: after rotation, assert a fully-consumed backup contributes
  zero rows to the cached result and that cached output equals `--no-cache`.*
- **AC-8** (Verifies FR-8, FR-9, INV-7): Given `--since 12h` (beyond the hot window), when
  the query executes, then the cold path scans raw files for the older range and
  the result includes records older than RETENTION.
  *Verification hint: `just otelq --format json errors --since 12h` returns
  pre-hot-window rows.*
- **AC-9** (Verifies FR-9): Given a windowless `summary`, when run with no flags,
  then it reports only the hot window; when run with `--all`, then it reports the
  full raw history.
  *Verification hint: compare `just otelq summary` vs `just otelq --all summary`
  time spans.*
- **AC-10** (Verifies FR-10): Given a `trace <id>` whose spans are older than the
  hot window, when run, then the hot lookup misses and the cold fallback finds and
  returns the full trace tree.
  *Verification hint: seal recent data, query an old trace id, assert spans
  returned.*
- **AC-11** (Verifies FR-11, INV-1, INV-4, INV-7): Given any command and range, when the
  same query runs with the cache and with `--no-cache`, then both return identical
  rows.
  *Verification hint: parametrized test diffing cached vs `--no-cache` JSON output
  across `summary`, `errors`, `slow`, `logs`, `trace`, `metric`.*
- **AC-12** (Verifies FR-12, INV-5, EC-3): Given two otelq processes started
  concurrently, when both attempt to seal, then exactly one writes cache state,
  both return correct results, and no partition is corrupted or duplicated.
  *Verification hint: spawn two runs against one temp corpus; assert parquet
  integrity and identical query output.*
- **AC-13** (Verifies FR-13): Given the supported platforms, when the cache code is
  inspected, then it uses `os.replace`, binary-mode reads, colon-free UTC minute
  keys, and `pathlib`, and imports no `fcntl` or text-mode offset tracking.
  *Verification hint: a static test asserting absence of `fcntl`/`os.rename`/text
  `tell()` and presence of `os.replace`; run the suite on Linux and macOS CI.*
- **AC-14** (Verifies FR-14, EC-7, EC-8): Given a `cursor.json` with an unknown
  version (or corrupt content) and a stray `*.tmp`, when otelq runs, then it wipes
  and rebuilds the cache, ignores/reaps the `*.tmp`, and returns a correct result.
  *Verification hint: write a bad cursor + a `.tmp`; run; assert rebuild and
  correct output.*
- **AC-15** (Verifies FR-15, EC-4, EC-5): Given a corpus with a truncated trailing
  line and one batch of >2048 records, when otelq runs incrementally, then the
  truncated line is skipped, the oversized batch is skipped with a warning, and the
  run succeeds.
  *Verification hint: fabricate both conditions in a temp corpus; assert exit
  success and warning on stderr.*
- **AC-16** (Verifies FR-16): Given a populated cache, when `just otel-clean` runs,
  then `telemetry/.otelq-cache/` no longer exists.
  *Verification hint: seed cache; run the recipe; assert the directory is gone.*
- **AC-17** (Verifies FR-17): Given `--no-cache`, when any command runs, then no
  cache files are read or written and the result is correct.
  *Verification hint: run with `--no-cache` against an empty cache dir; assert the
  dir stays empty and output is correct.*
- **AC-18** (Verifies INV-6, EC-10): Given any otelq run (including one where a raw
  backup disappears mid-run), when it completes, then no `*.jsonl` raw file has
  been modified or deleted by otelq and the run does not crash.
  *Verification hint: checksum raw files before/after; delete a backup during a
  run and assert clean completion.*
- **AC-19** (Verifies INV-2, EC-9, EC-11): Given an empty cache, when otelq runs,
  populates the cache, and is then re-run after a simulated crash (cursor not
  advanced past a sealed minute), then sealed partitions are unchanged
  (immutable), no record is missing or duplicated, and results stay equivalent to
  `--no-cache`.
  *Verification hint: seal, snapshot partition bytes, roll back the cursor, re-run,
  assert partitions byte-identical and output equivalent.*
- **AC-20** (Verifies FR-3, EC-6): Given a fabricated corpus on which `st_ino` is
  forced to `0` (or to a reused value) for the raw files, when otelq ingests, then
  the fingerprint and size still distinguish the files, and the query result stays
  identical to `--no-cache`.
  *Verification hint: monkeypatch `os.stat` to zero `st_ino` in a unit test; assert
  correct sealing and cached/`--no-cache` equivalence.*

### Examples

- **Rotation mid-minute (EC-1).** Active `traces.jsonl` holds records for
  `10:29:30–10:30:20` and is consumed to `10:30:05`'s offset. A size rotation
  renames it to `traces-<ts>.jsonl` and starts a new `traces.jsonl` that receives
  `10:30:21–…`. The next run resumes the backup from the stored offset, reads the
  new active file from zero, and seals minute `10:30` containing records from both
  files.
- **Equivalence keystone (FR-11).** `just otelq --format json errors --since 20m`
  and `just otelq --no-cache --format json errors --since 20m` return byte-identical
  JSON.

## Invariants

- **INV-1** — The cursor governs only sealing. A query's reachability of any record
  never depends on cursor state.
- **INV-2** — A sealed parquet partition is immutable: once written it is never
  modified, only deleted by eviction.
- **INV-3** — Only minutes strictly behind the per-signal watermark by at least
  `MARGIN` are sealed.
- **INV-4** — Queryable history equals raw-file history: the cache never makes a
  record unreachable that a full raw scan over the same range would return.
- **INV-5** — At most one process writes cache state (sealing, eviction, cursor) at
  a time; readers never block on the writer.
- **INV-6** — otelq never modifies or deletes raw `*.jsonl` files.
- **INV-7** — Query time windows (the default hot window and any `--since`) are
  measured relative to the maximum observed event-time, not the host wall-clock,
  and the identical basis is applied on both the hot and the cold path.
