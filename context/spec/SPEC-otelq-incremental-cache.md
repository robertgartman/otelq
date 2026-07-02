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
last_updated: 2026-07-02
related_documents:
  - ADR-008-unified-cache-first-read-and-retention
  - ADR-005-incremental-parquet-cache
  - ADR-004-collector-in-docker-bind-mount
  - SPEC-otelq-cli
ai_summary: "otelq's incremental parquet cache: cache-first gap-filled reads, seal-what-you-parse, signal-scoped maintenance, 24h event-time retention, raw-equivalent within raw's coverage."
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
`.telemetry/*.jsonl` corpus on every invocation — while returning results
identical to the current full-scan tool.

Architectural context is recorded in
[ADR-008](../adr/ADR-008-unified-cache-first-read-and-retention.md) (the
governing decision: unified cache-first reads, seal-what-you-parse, signal
scoping, and extended event-time retention), its superseded predecessor
[ADR-005](../archive/ADR-005-incremental-parquet-cache.md) (the original cache
mechanics — cursor, margin sealing, watermark, lock — which ADR-008 carries
forward), and [ADR-004](../adr/ADR-004-collector-in-docker-bind-mount.md)
(the Collector file-export seam).
This document specifies *what the cache must do*, not why the approach was chosen.

## Scope

**Covered:** how otelq persists telemetry to a parquet cache, when it seals and
evicts cached data, how it routes a query between the cache and the raw files,
how it stays correct under rotation, concurrency, crashes, and version changes,
and how it behaves identically across supported platforms.

**Not covered:** the OTel Collector configuration and rotation settings (an input,
not changed here); the `duckdb-otlp` reader extension; the set of signals, columns,
and views exposed to queries (unchanged from the current tool); and the design
rationale for the cache (see
[ADR-008](../adr/ADR-008-unified-cache-first-read-and-retention.md)).

### Definitions

- **Raw files** — the Collector's append-only, size-rotated JSONL outputs
  (`.telemetry/<signal>.jsonl` active file + `.telemetry/<signal>-<ts>.jsonl`
  rotated backups). otelq treats these as read-only inputs.
- **Signal** — one of the six cache signals: `traces`, `logs`, `metrics_gauge`,
  `metrics_sum`, `metrics_histogram`, `metrics_exp_histogram`. The single
  `metrics` raw byte-stream feeds the four per-type metric signals (one
  `read_otlp_metrics_<type>` reader each); the cursor tracks bytes and a
  watermark per raw stream, while sealed parquet partitions are kept per signal.
- **Event-time** — a record's own timestamp (`timeUnixNano`), never wall-clock.
- **Minute M** — the UTC clock-minute `[M, M+1)`, keyed filename-safe as
  `<date>T<hour>-<minute>` (e.g. `2026-06-22T10-30`; colon-free).
- **Cache** — `.telemetry/.otelq-cache/`, containing per-signal sealed parquet
  partitions `<signal>/<minute>.parquet`, an unsealed-tail `pending` parquet per
  signal, and a `cursor.json` state file.
- **RETENTION_HORIZON** — the event-time age beyond which sealed partitions are
  evicted; default **24 hours**. (Replaces the former ~30-minute rolling window
  per [ADR-008](../adr/ADR-008-unified-cache-first-read-and-retention.md).)
- **DEFAULT_WINDOW** — the trailing event-time window a command without an
  explicit range queries; default **30 minutes**. A query-scope default only — it
  no longer bounds what is sealed or retained.
- **MARGIN** — the watermark lateness allowance; default **2 minutes**.
- **MAX_FUTURE_SKEW** — the tolerance beyond host wall-clock allowed for an
  event-time anchor; an observed watermark further ahead than this is clamped for
  windowing, eviction, and ingest-floor purposes (EC-12, INV-7); default **1 day**.
- **Watermark (per signal)** — the maximum event-time otelq has observed for that
  signal across all ingest so far.
- **Covered minute** — a minute that has a sealed parquet partition for the
  signal being read.
- **Pending tail** — the per-signal parquet holding consumed records whose minute
  is not yet sealable (the current partial minute plus minutes still within
  `MARGIN` of the watermark, and late arrivals for already-sealed minutes).
- **Cache-first read** — the single read path: for the queried range, covered
  minutes are served from sealed parquet (plus the pending tail), and only the
  uncovered minutes are gap-filled from a raw scan.
- **Pure-raw path** — the `--no-cache` scan of the raw files only, reading and
  writing no cache or cursor state; the correctness oracle (FR-11).

## Functional Requirements

- **FR-1 — Cache store layout.** otelq **must** persist sealed telemetry as
  `.telemetry/.otelq-cache/<signal>/<minute>.parquet` for each signal, alongside a
  per-signal pending-tail parquet and a single
  `.telemetry/.otelq-cache/cursor.json` state file.
- **FR-2 — Incremental ingest.** On each run, ingest **must** read only the
  raw bytes not yet recorded as consumed by the cursor, and **must not** re-parse
  raw data already consumed in a previous run.
- **FR-3 — Cursor and rotation tolerance.** The cursor **must** identify each raw
  file by `(inode, first-256-byte fingerprint)` mapped to a `bytes_consumed`
  offset. File size **must not** be part of the identity key (an appended-to file
  keeps its identity across runs); instead, when a tracked file's current size is
  **smaller** than its recorded `bytes_consumed`, otelq **must** treat it as a new
  file and re-read it from offset `0`, so an in-place truncation or replacement is
  never partially skipped. A size-triggered rotation that renames the active file
  to a backup **must** cause neither re-parsing of already-consumed bytes nor loss
  of not-yet-consumed bytes. When a tracked raw file cannot be opened on a given
  run (transient error), its prior cursor entry **must** be carried forward
  unchanged rather than dropped, so a later run neither re-reads consumed bytes nor
  loses the entry.
- **FR-4 — Per-signal watermark sealing.** A minute `M` **must** be sealed for a
  signal only once that signal's watermark has reached `M + 1 minute + MARGIN`
  (i.e. all of minute `M` lies at least `MARGIN` behind the latest observed
  event-time). Signals **must** seal independently of one another.
- **FR-5 — Complete sealing; seal what you parse.** A sealed parquet partition
  **must** contain every record for that minute consumed up to the moment it
  sealed (sealing only once the watermark has passed the minute's end by `MARGIN`,
  so it is complete under normal arrival ordering). A record arriving for an
  already-sealed minute (late/out-of-order beyond `MARGIN`) **must** be retained in
  the pending tail rather than added to the immutable partition, so a cache-first
  read still returns it (FR-11). Every sealable complete minute whose records
  otelq parses to answer a query — regardless of the minute's age — **must** be
  sealed, so a wide query warms the cache for the whole range it touched
  ([ADR-008](../adr/ADR-008-unified-cache-first-read-and-retention.md) decision 2;
  retires the former "seal only within the hot window" bound).
- **FR-6 — Retention and eviction.** otelq **must** remove sealed parquet
  partitions whose minute is older than `RETENTION_HORIZON` measured against the
  clamped event-time watermark (INV-7), so the cache footprint stays bounded to
  approximately `RETENTION_HORIZON` per signal. Eviction **must** run for every
  signal that has any sealed partitions, independent of whether that signal
  received new records on the current run, so an idle signal's stale partitions are
  still reclaimed. Eviction **must** be decoupled from sealing (it is a cheap
  unlink of out-of-horizon partitions, not conditional on new ingest).
- **FR-7 — Cache-first, gap-filled read.** For any command and time range, the
  answer **must** be resolved from the union of sealed parquet partitions (for
  covered minutes), the pending tail, and a raw scan restricted to the uncovered
  minutes of the range. There **must** be no window threshold that switches the
  cache off. The union **must** partition minutes cleanly: a minute served from a
  sealed partition **must not** also contribute rows from raw (no double-count,
  no gap). A fully-consumed rotated backup whose minutes are all covered
  contributes zero re-parsed rows to the query relations (the incremental cursor
  yields an empty delta for it; the cursor may still stat and read a backup's
  prefix bytes to confirm its identity).
- **FR-8 — Answer-first ordering; no daemon.** The answer to the current query
  **must** be produced and returned from `cache ∪ raw` before any sealing of
  newly-parsed minutes is persisted; sealing benefits only future queries.
  Maintenance (sealing, eviction, cursor writes) **must** run within the same
  CLI invocation — otelq **must not** spawn a background daemon, listener, or
  detached process to do it
  ([ADR-008](../adr/ADR-008-unified-cache-first-read-and-retention.md) decision 5,
  preserving [ADR-001](../adr/ADR-001-host-cli-reads-bind-mounted-files.md)).
- **FR-9 — Recent-by-default query scope.** Commands without an explicit time
  window (`summary`, `slow`, `logs`, `metric`, `sql`) **must** default to the
  trailing `DEFAULT_WINDOW` of event-time. A new `--all` global flag **must**
  widen the query to the full raw history, and an explicit `--since` **must** set
  the window to the requested span; both are answered by the same cache-first,
  gap-filled read (FR-7).
- **FR-10 — Lookup routing.** `trace <id>` **must** first search the default
  window and, on a miss, **must** widen to the full available history (a trace is
  identity-addressed, not time-scoped) — still via the cache-first read.
  `metric <name>` **must not** widen its window on an empty result; an empty
  result is a valid answer for the requested window, and coverage widens only when
  the user explicitly requests it (`--all` or a wider `--since`).
- **FR-11 — Result equivalence within raw coverage.** For any command and time
  range, over the range that the raw files still cover, results produced with the
  cache **must** be identical to results produced by a pure-raw scan
  (`--no-cache`) of the same range. Where raw backups have been rotated away by
  the Collector while the cache still holds those minutes (the cache outlives
  raw; `RETENTION_HORIZON` exceeds raw rotation depth), the cached result **may**
  include those additional records that raw no longer has — never fewer records
  than raw, and never different values for records raw still holds. Within raw's
  coverage the cache is an accelerator only; it **must not** change which records
  a query returns.
- **FR-12 — Single-writer concurrency.** Sealing, eviction, and cursor writes
  **must** be serialized by an advisory lock implemented without `fcntl` (e.g. an
  `O_EXCL` sentinel file with a stale-lock reaper). A run that cannot acquire the
  lock **must** still answer its query (from existing parquet plus a raw tail
  scan) and **must** skip sealing rather than block or fail. Readers **must not**
  block on the writer. The stale-lock reaper **must not** remove a lock whose
  recorded pid is live and whose age is within the hard ceiling (a genuine
  long-running catch-up seal), and lock release **must** verify the sentinel still
  holds this process's own pid before unlinking, so a reaped-then-reacquired lock
  belonging to another run is never deleted. Reaping a stale `*.tmp` **must not**
  remove a temp file that a live writer currently holds the lock for.
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
  also remove `.telemetry/.otelq-cache/`, so a clean is a full reset.
- **FR-17 — `--no-cache` bypass.** A `--no-cache` global flag **must** force a
  pure-raw scan that neither reads nor writes the cache, for debugging and for
  equivalence verification (FR-11).
- **FR-18 — Signal-scoped maintenance.** Ingest and sealing **must** run only for
  the raw signal stream(s) the current command actually reads: a `logs` query
  does not parse or seal traces or metrics; commands that span all signals
  (e.g. `summary`, `sql`) scope to all streams. Because the single `metrics` raw
  byte-stream feeds all four per-type metric signals, scoping is per raw
  stream, not per metric type. Eviction is exempt from this scoping and runs per
  FR-6 for every signal with sealed partitions.
- **FR-19 — Empty-delta short-circuit.** When every raw stream in scope yields an
  empty incremental delta (no new bytes since the cursor position), otelq
  **must** skip ingest, sealing, and the pending-tail rewrite entirely and answer
  from the existing sealed partitions and pending tail, so a rapid burst of
  queries over unchanged data performs zero cache-write work. The freshest,
  not-yet-sealable records (the current partial minute plus minutes within
  `MARGIN`) **must** be served from the persisted pending tail rather than
  re-read from raw, so a query touching "now" parses only the raw delta since the
  previous invocation's cursor position.

## Edge Cases & Failure Modes

- **EC-1 — Rotation mid-minute.** A minute whose records are split across the
  active file and a freshly-rotated backup is still sealed as one complete minute.
- **EC-2 — Idle then run.** After a long gap with no otelq invocation, a single
  run performs a larger catch-up parse to advance the cursor; results are correct
  and every sealable minute parsed within the query's scope is sealed (FR-5),
  bounded by `RETENTION_HORIZON`.
- **EC-3 — Concurrent runs.** Two runs that would seal the same minute do not
  corrupt or duplicate the partition; sealing is idempotent via atomic replace.
- **EC-4 — Partial trailing line.** A half-written final JSONL line is skipped and
  the run still succeeds.
- **EC-5 — Oversized batch.** A single export batch exceeding 2048 records is
  skipped with a warning, as today.
- **EC-6 — Unreliable inode.** On filesystems where `st_ino` is `0` or
  non-unique (e.g. FAT32, some network shares), or after inode reuse, the
  fingerprint disambiguates; the worst outcome is a safe re-read, never
  incorrect data.
- **EC-7 — Cache version mismatch or corruption.** A `cursor.json` with an
  unknown version or unreadable content triggers a self-wipe and rebuild; the
  query still returns correct results.
- **EC-8 — Stale temp file.** A `*.tmp` left by a crashed write is never served
  and is reaped.
- **EC-9 — Empty or cold cache.** The first run (or the first run after
  `otel-clean`) answers correctly by gap-filling the entire range from raw
  (no minute is covered yet) and begins populating the cache.
- **EC-10 — Backup evicted by the Collector.** If the Collector's `max_backups`
  deletes a raw backup before otelq consumes it, the cache holds nothing the raw
  files dropped and otelq does not crash; the reported range reflects available
  raw data.
- **EC-11 — Crash between seal and cursor advance.** If a run dies after sealing a
  minute but before persisting the advanced cursor, the next run re-derives state
  safely: sealed partitions stay immutable (idempotent skip) and no record is
  lost. A crash in the narrow window mid-ingest may transiently leave the most
  recent unsealed minutes over-counted in the cache; a `--no-cache` query is always
  exact and `otel-clean` clears the cache. In normal, crash-free operation the
  cache-first read is exactly equal to a full raw re-scan over raw's coverage
  (FR-11), including out-of-order and byte-identical duplicate records.
- **EC-12 — Divergent clocks.** Sealing and the watermark are driven by record
  event-time, so otelq running on a host whose wall-clock differs from the
  applications' still seals the correct minutes. To bound a poison record whose
  event-time is implausibly far in the future, the ingest floor and query window
  anchor derived from observed event-time **must** be clamped to
  `wall_clock + MAX_FUTURE_SKEW` before use, so a single far-future outlier cannot
  yank the watermark (evicting or hiding real recent data). The clamp is applied
  identically on the cache-first and `--no-cache` paths, preserving FR-11; the
  true (unclamped) watermark is still persisted for cursor fidelity.
- **EC-13 — Far-future poison record.** A single record whose event-time is days
  ahead of every other record **must not** cause real, recently-arrived records to
  be evicted from the cache or excluded from a default-window query; the window
  anchor is clamped per EC-12 and the outlier itself is simply out of the clamped
  window.
- **EC-14 — Empty metric result in window.** A `metric <name>` query that matches
  no samples within the queried window returns an empty result for that window
  without silently widening to the full raw history; the user widens explicitly
  via `--all` or `--since`.
- **EC-15 — Truncated / replaced raw file.** A tracked raw file whose size shrinks
  below its recorded `bytes_consumed` (in-place truncation or replacement) is
  re-read from offset `0` rather than partially skipped, and the cache remains
  equivalent to a raw re-scan.
- **EC-16 — Transient raw-file open failure.** If a tracked raw file cannot be
  opened on a run, its cursor entry is carried forward unchanged; a later
  successful run neither re-reads already-consumed bytes nor loses the entry.
- **EC-17 — Lock reacquired by another run.** A lock reaped as stale and then
  reacquired by a different run is not deleted by the original holder's release
  (pid is verified before unlink), and a live writer's `*.tmp` is not reaped out
  from under it.
- **EC-18 — Cache outlives raw backups.** When the Collector's `max_backups`
  rotation deletes raw files whose minutes are already sealed, a query over that
  range returns the cached records even though a `--no-cache` scan no longer can;
  over the range raw still covers, the two remain identical (FR-11).
- **EC-19 — Mixed covered/uncovered window.** A query spanning both sealed
  minutes and minutes with no partition returns each minute exactly once: covered
  minutes from parquet, uncovered minutes from raw, with no duplicated and no
  dropped records at the boundaries (FR-7).
- **EC-20 — Rapid burst over unchanged data.** A series of back-to-back queries
  with no new raw bytes between them each answers from the existing
  `sealed ∪ pending` and performs no ingest, sealing, or pending rewrite (FR-19);
  results are identical across the burst.
- **EC-21 — First wide query of an investigation.** The first query over a wide,
  previously-unsealed range pays the raw parse and the sealing backfill once
  (after the answer is returned, FR-8); repeated queries over the same range are
  then served from parquet plus at most the fresh raw delta.

## Acceptance Criteria

> Given/When/Then, each independently testable. New cache behavior is exercised
> with **file-based** fixtures — fabricated OTLP JSONL written to a temporary
> `telemetry` directory — in contrast to the existing in-memory `synth_conn`
> fixture, because the cache operates on files. Hints reference `just otelq`,
> `just otelq-test`, and `tests/test_otelq.py`.

- **AC-1** (Verifies FR-1, FR-5): Given fabricated traces spanning three complete,
  sealable minutes, when otelq runs once, then
  `.telemetry/.otelq-cache/traces/<minute>.parquet` exists for each sealed minute
  and each file contains exactly that minute's records.
  *Verification hint: run a command via the cache, then list the cache dir and
  read each parquet with DuckDB `read_parquet`.*
- **AC-2** (Verifies FR-2): Given a populated cache, when otelq runs again with no
  new raw bytes appended, then it parses zero raw records (the answer comes from
  sealed parquet plus the persisted pending tail).
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
- **AC-5** (Verifies FR-5, EC-2, EC-21): Given a cold start where a wide query
  (`--all` or a long `--since`) parses raw data spanning many hours, when otelq
  runs once, then every complete, sealable minute the query parsed within
  `RETENTION_HORIZON` is sealed to parquet, and a repeat of the same query parses
  at most the fresh raw delta.
  *Verification hint: fabricate 3h of data; run `--since 3h` twice; assert sealed
  partition count ≈ the parsed span and the second run's consumed-byte count ≈ 0.*
- **AC-6** (Verifies FR-6): Given a cache containing partitions older than
  `RETENTION_HORIZON` (measured against the clamped watermark), when otelq runs,
  then those partitions are removed and only partitions within the horizon remain.
  *Verification hint: pre-seed out-of-horizon parquet files; run; assert they are
  gone.*
- **AC-7** (Verifies FR-7): Given a query over a range whose minutes are all
  covered by sealed partitions, when it executes, then the result is built from
  sealed parquet plus the pending tail, and no rows
  are re-parsed from a fully-consumed rotated backup into the query relations (the
  incremental cursor yields an empty delta for it; the cursor may still stat and
  read a backup's prefix bytes to confirm its identity).
  *Verification hint: after rotation, assert a fully-consumed backup contributes
  zero rows to the cached result and that cached output equals `--no-cache`.*
- **AC-8** (Verifies FR-7, FR-9, INV-7): Given `--since 12h` (beyond the default
  window), when the query executes, then the cache-first read gap-fills the
  uncached range from raw and the result includes records older than
  `DEFAULT_WINDOW`.
  *Verification hint: `just otelq --format json --since 12h errors` returns
  rows older than the default window.*
- **AC-9** (Verifies FR-9): Given a windowless `summary`, when run with no flags,
  then it reports only the trailing `DEFAULT_WINDOW`; when run with `--all`, then
  it reports the full raw history.
  *Verification hint: compare `just otelq summary` vs `just otelq --all summary`
  time spans.*
- **AC-10** (Verifies FR-10): Given a `trace <id>` whose spans are older than the
  default window, when run, then the default-window lookup misses and the widened
  lookup finds and returns the full trace tree.
  *Verification hint: seed recent data, query an old trace id, assert spans
  returned.*
- **AC-11** (Verifies FR-11, INV-1, INV-4, INV-7): Given any command and range
  over data the raw files still cover, when the same query runs with the cache and
  with `--no-cache`, then both return identical rows.
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
  then `.telemetry/.otelq-cache/` no longer exists.
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
  the fingerprint still distinguishes the files, and the query result stays
  identical to `--no-cache`.
  *Verification hint: monkeypatch `os.stat` to zero `st_ino` in a unit test; assert
  correct sealing and cached/`--no-cache` equivalence.*
- **AC-21** (Verifies EC-12, EC-13, INV-7): Given a corpus in which one record's
  event-time is days in the future while the rest are recent, when a default-window
  query runs with the cache and with `--no-cache`, then real recent records are still
  returned (not evicted or hidden by the outlier), the window anchor is clamped to
  `wall_clock + MAX_FUTURE_SKEW`, and cached output equals `--no-cache`.
  *Verification hint: `tests/test_otelq.py::test_clock_skew_outlier_does_not_drop_records`
  and `::test_b1_window_anchor_clamped_to_ceiling`.*
- **AC-22** (Verifies FR-10, EC-14): Given a `metric <name>` query matching no
  samples in the default window, when it runs without `--all`/`--since`, then it
  returns an empty result for that window and does not silently widen to the full
  history.
  *Verification hint: seed a metric outside the default window; assert the default
  query is empty and `--all` surfaces it.*
- **AC-23** (Verifies FR-3, EC-15): Given a tracked raw file that is truncated (or
  replaced) so its size drops below the recorded `bytes_consumed`, when otelq runs
  again, then it re-reads the file from offset `0` and the cached result equals
  `--no-cache`.
  *Verification hint: consume a file, truncate it in place, re-run, assert
  equivalence.*
- **AC-24** (Verifies FR-3, EC-16): Given a tracked raw file that cannot be opened
  on one run, when a later run succeeds, then the file's cursor entry was carried
  forward (not dropped) and no already-consumed bytes are re-read.
  *Verification hint: simulate an open failure for one file; assert the cursor entry
  persists and offsets are unchanged.*
- **AC-25** (Verifies FR-6): Given a signal with sealed partitions that receives no
  new records on a later run, when eviction runs, then that signal's out-of-window
  partitions are still removed.
  *Verification hint: seed old partitions for an idle signal; run; assert they are
  evicted.*
- **AC-26** (Verifies FR-12, INV-5, EC-17): Given a live lock within the hard
  ceiling, a lock past the hard ceiling, and a dead-pid lock, when the reaper runs,
  then only the latter two are reaped; and lock release only unlinks a sentinel
  still holding this process's pid.
  *Verification hint: `tests/test_otelq.py::test_live_lock_reaped_only_past_hard_ceiling`.*
- **AC-27** (Verifies FR-7, EC-19): Given a query window spanning both sealed
  minutes and uncovered minutes, when it executes, then each record appears
  exactly once — covered minutes from parquet, uncovered minutes from raw — with
  no duplicate and no missing records at the coverage boundaries, and the output
  equals `--no-cache` over the same range.
  *Verification hint: seal part of a range, delete the cursor coverage for the
  rest, run a spanning query, diff against `--no-cache`.*
- **AC-28** (Verifies FR-8): Given a query that parses new sealable minutes, when
  it runs, then the result is computed and rendered before the new partitions are
  persisted, and no child process outlives the invocation.
  *Verification hint: instrument the seal step to record ordering relative to
  result rendering; assert no lingering process after exit.*
- **AC-29** (Verifies FR-18): Given a corpus with traces, logs, and metrics, when
  a `logs` query runs, then only the logs raw stream is ingested and sealed (no
  trace or metric partitions are written by that run), while a `summary` run
  ingests all streams.
  *Verification hint: run `logs` on a fresh cache; assert only
  `.otelq-cache/logs/` gains partitions; then run `summary` and assert the rest.*
- **AC-30** (Verifies FR-19, EC-20): Given a populated cache and pending tail,
  when the same query runs repeatedly with no new raw bytes appended between
  runs, then no cache file (partition, pending, cursor) is rewritten by the
  repeat runs and the results are identical across the burst.
  *Verification hint: snapshot cache-dir mtimes/bytes between back-to-back runs;
  assert unchanged and outputs identical.*
- **AC-31** (Verifies FR-11, FR-6, EC-18): Given sealed minutes whose raw backups
  are then deleted (simulating Collector `max_backups` rotation), when a query
  over that range runs with the cache, then the cached records for the rotated
  minutes are still returned; and over the surviving raw range the cached output
  equals `--no-cache`.
  *Verification hint: seal a range, delete a raw backup, run cached and
  `--no-cache` queries, assert cache ⊇ raw and equality over raw's coverage.*

### Examples

- **Rotation mid-minute (EC-1).** Active `traces.jsonl` holds records for
  `10:29:30–10:30:20` and is consumed to `10:30:05`'s offset. A size rotation
  renames it to `traces-<ts>.jsonl` and starts a new `traces.jsonl` that receives
  `10:30:21–…`. The next run resumes the backup from the stored offset, reads the
  new active file from zero, and seals minute `10:30` containing records from both
  files.
- **Equivalence keystone (FR-11).** `just otelq --format json --since 20m errors`
  and `just otelq --no-cache --format json --since 20m errors` return byte-identical
  JSON while the raw files still cover the window.
- **Investigation burst (FR-5, FR-7, EC-21).** `just otelq --since 1h errors`
  pays the raw parse once and seals the hour; the follow-up
  `just otelq --since 1h slow` and `just otelq --since 1h logs` queries in the
  same investigation are served from parquet plus at most the fresh raw delta.

## Invariants

- **INV-1** — The cursor governs only sealing. A query's reachability of any record
  never depends on cursor state.
- **INV-2** — A sealed parquet partition is immutable: once written it is never
  modified, only deleted by eviction.
- **INV-3** — Only minutes strictly behind the per-signal watermark by at least
  `MARGIN` are sealed.
- **INV-4** — Queryable history is a superset of raw-file history: the cache never
  makes a record unreachable that a full raw scan over the same range would
  return, and within raw's coverage it never adds, drops, or alters a record
  (FR-11). Beyond raw's coverage it may retain records raw has rotated away.
- **INV-5** — At most one process writes cache state (sealing, eviction, cursor) at
  a time; readers never block on the writer.
- **INV-6** — otelq never modifies or deletes raw `*.jsonl` files.
- **INV-7** — Query time windows (the trailing `DEFAULT_WINDOW` and any `--since`)
  are measured relative to the maximum observed event-time, not the host
  wall-clock, and the identical basis is applied on the cache-first and
  `--no-cache` paths. The event-time anchor used for the ingest floor, eviction,
  and the query window is clamped to `wall_clock + MAX_FUTURE_SKEW` so a single
  implausibly-far-future record cannot advance the effective watermark; the true
  watermark is still persisted for cursor fidelity (EC-12).
