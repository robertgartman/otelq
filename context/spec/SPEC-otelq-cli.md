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
  - external_data_schemas
created: 2026-06-23
last_updated: 2026-06-25
related_documents:
  - PRD-otelq
  - SPEC-otelq-incremental-cache
  - CONTRACT-telemetry-directory
  - ADR-006-read-otlp-extension-quirks
ai_summary: "otelq CLI base behavior: the query relations/columns it exposes, its seven subcommands, global flags and argument order, and its friendly read-only failure handling."
semantic_tags:
  - otelq
  - cli
  - telemetry
  - duckdb
  - traces
  - logs
  - metrics
  - observability
---

# SPEC — otelq CLI (Base Behavior)

## Purpose

Define the exact, testable behavior of the `otelq` command-line tool
(`otelq.py`): the query relations and columns it exposes, the
seven subcommands and their output, the global flags and their argument-order
rule, and its robust, read-only handling of absent or malformed telemetry. This
is the base CLI contract on which the incremental cache builds.

This document specifies the CLI's externally observable behavior. The on-disk
telemetry directory and OTLP JSONL layout that otelq reads are defined in
[CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md) and
**must not** be redefined here. The cache that accelerates repeated and recent
queries is specified in
[SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md); the quirks of
the `duckdb-otlp` reader extension that otelq compensates for are recorded in
[ADR-006-read-otlp-extension-quirks](../adr/ADR-006-read-otlp-extension-quirks.md).
Product intent lives in [PRD-otelq](../prd/PRD-otelq.md).

## Scope

**Covered:** the named query relations/views and their column sets as exposed to
`sql` and the built-in commands; the behavior, inputs, and output columns of the
seven subcommands (`summary`, `errors`, `slow`, `trace`, `logs`, `metric`,
`sql`); the global flags (`--format`, `--dir`, `--all`, `--no-cache`, `--since`)
and the rule that global flags precede the subcommand; the three output formats
and their format-independence; timestamp correction in the presented output; and
otelq's exit-code and stderr behavior when telemetry is absent, partial, or
malformed.

**Not covered:** the raw telemetry directory and OTLP JSONL schema (an external
input — see [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md));
the parquet cache mechanics, sealing, eviction, and hot/cold routing (see
[SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md)); the
`duckdb-otlp` extension itself and the rationale for working around it (see
[ADR-006](../adr/ADR-006-read-otlp-extension-quirks.md)); and the OTel Collector
configuration that produces the raw files.

### Definitions

- **Relation / view** — a queryable table or view name exposed to SQL:
  `traces`, `logs`, `metrics`, `metrics_gauge`, `metrics_sum`.
- **Signal** — a user-facing telemetry kind: `traces`, `logs`, `metrics`.
- **Subcommand / command** — one of the seven verbs otelq accepts
  (`summary`, `errors`, `slow`, `trace`, `logs`, `metric`, `sql`).
- **Global flag** — a flag accepted before the subcommand (`--format`, `--dir`,
  `--all`, `--no-cache`, `--since`); contrast subcommand-specific flags
  (`--top`, `--service`, `--level`, `--grep`, the `trace_id`/`name`/`query`
  positionals), which follow the subcommand.
- **Default telemetry dir** — the `telemetry/` directory resolved relative to
  the `otelq.py` script location (two parents up, per
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)),
  used when `--dir` is not given.
- **Event-time** — a record's own timestamp, as corrected and presented in the
  `timestamp` column (see FR-16).
- **Result** — the `(columns, rows)` pair a command produces, rendered by the
  selected output format.

## Functional Requirements

### Query relations and columns

- **FR-1 — Exposed relations.** otelq **must** expose exactly these query
  relations over the captured telemetry: `traces`, `logs`, `metrics`,
  `metrics_gauge`, and `metrics_sum`. `metrics` **must** be the union of
  `metrics_gauge` and `metrics_sum` (whichever are present). All five **must** be
  queryable by the `sql` command; the built-in commands query the subset they
  need.
- **FR-2 — Relation columns.** Each relation **must** present at least the
  following columns (the `sql` cheat-sheet), with `timestamp` carrying the
  corrected wall-clock event-time (FR-16):
  - **`traces`**: `timestamp`, `duration` (nanoseconds), `trace_id`, `span_id`,
    `parent_span_id`, `service_name`, `span_name`, `span_kind`, `status_code`
    (`0`=unset, `1`=ok, `2`=error), `status_message`.
  - **`logs`**: `timestamp`, `trace_id`, `service_name`, `severity_text`,
    `severity_number`, `body`. `severity_number` is the OTel numeric severity;
    otelq maps it to a canonical level for `summary` (FR-3) using the standard
    ranges **TRACE** `1–4`, **DEBUG** `5–8`, **INFO** `9–12`, **WARN** `13–16`,
    **ERROR** `17–20`, **FATAL** `21–24` (values outside `1–24`, including `0`
    and null, are **UNSET**). The level is derived from `severity_number`, not
    the free-form `severity_text`, which carries inconsistent casing in practice
    (e.g. `Info`).
  - **`metrics`** (and the underlying `metrics_gauge` / `metrics_sum`):
    `timestamp`, `service_name`, `metric_name`, `metric_type`, `value`,
    `metric_unit`. `metric_type` **must** be `gauge` for rows originating from
    `metrics_gauge` and `sum` for rows from `metrics_sum`.

  The precise field semantics of the underlying raw records are owned by
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md);
  this requirement fixes only the column names otelq surfaces and the
  enumerations it relies on.

### The seven commands

- **FR-3 — `summary`.** `summary` **must** report a per-signal breakdown whose
  columns are, in order, `signal`, `details`, `count`, earliest `timestamp`,
  latest `timestamp`, and the distinct `service_name` count. Every numeric/time
  column **must** be scoped to its own row's subset (the count, span, and
  service count of just those records). Rows are produced **only for present
  signals**, as follows:
  - **`traces`** (when present): exactly two rows that partition spans by
    `duration` — `details = ">1s"` for `duration > 1s` (`> 1e9` ns) and
    `details = "=<1s"` for the remainder. **Both** rows **must** appear even when
    a bucket's count is zero.
  - **`logs`** (when present): one row per canonical severity level
    (`TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR`, `FATAL`), the level derived from
    `severity_number` per the ranges in FR-2. **All six** rows **must** appear
    even at zero count. Log records whose `severity_number` is outside the
    canonical ranges **must** contribute an additional `details = "UNSET"` row,
    shown **only** when its count is non-zero.
  - **`metrics`** (when present): a single row with an empty `details` (metrics
    have no meaningful sub-categorization here).

  A signal with **no captured data** contributes **no rows** (its zero-count
  skeleton is not emitted). When **no** signal is present at all, the friendly
  empty-telemetry behavior applies (FR-18). The zero-count rule thus governs
  *sub-rows within a present signal* (e.g. an `ERROR` level with no records), not
  absent signals.
- **FR-4 — `errors`.** `errors` **must** return error-status spans
  (`traces` rows with `status_code == 2`) and error/fatal logs (`logs` rows with
  `severity_text` in `{ERROR, FATAL}`), combined into one result and ordered
  newest-first by `timestamp`. Each row **must** identify whether it is a span or
  a log.
- **FR-5 — `slow`.** `slow` **must** return spans ordered by `duration`
  descending, limited to the top `N` where `N` is the value of `--top`
  (default **20**). The presented duration **must** be expressed in milliseconds.
- **FR-6 — `trace <trace_id>`.** `trace` **must** take a `trace_id` positional
  argument and return every span of that trace arranged as a parent/child tree
  (each span ordered under its parent by `timestamp`, with a depth indicator). A
  span whose `parent_span_id` is absent or not present among the trace's own
  spans **must** be treated as a root.
- **FR-7 — `logs`.** `logs` **must** return log records ordered newest-first by
  `timestamp`, filtered by the optional subcommand flags `--service`
  (exact `service_name`), `--level` (exact `severity_text`, case-insensitive
  input), and `--grep` (case-insensitive substring of `body`). With no filter
  flags it **must** return all in-window log records.
- **FR-8 — `metric <name>`.** `metric` **must** take a `name` positional
  argument and return the time series for that metric (`metrics` rows whose
  `metric_name` equals `name`) ordered ascending by `timestamp`.
- **FR-9 — `sql "<query>"`.** `sql` **must** take a SQL string positional
  argument, execute it against the exposed relations (FR-1), and return its
  columns and rows. A SQL execution error **must** be reported as a real error
  (FR-17), not swallowed.

### Global flags and argument order

- **FR-10 — `--format`.** A `--format` global flag **must** accept exactly
  `table`, `json`, or `csv`, defaulting to `table`, and **must** select the
  rendering of the result. `table` is for human reading; `json` is for
  programmatic consumption.
- **FR-11 — Global flags precede the subcommand.** `--format`, `--dir`,
  `--all`, `--no-cache`, and `--since` are global flags and **must** be accepted
  *before* the subcommand. Supplying a global flag *after* the subcommand
  **must** be rejected as an unrecognized argument (a hard parse error), not
  silently accepted. Subcommand-specific flags and positionals continue to follow
  the subcommand.
- **FR-12 — `--dir`.** A `--dir <path>` global flag **must** select the
  telemetry directory to read; when omitted, otelq **must** read the default
  telemetry dir (see Definitions).
- **FR-13 — `--all`.** An `--all` global flag **must** widen the query to the
  full raw history. (The routing this triggers is specified in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-9.)
- **FR-14 — `--no-cache`.** A `--no-cache` global flag **must** force a pure cold
  scan of the raw files that neither reads nor writes any cache. (Cache
  interaction is specified in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-17.)
- **FR-15 — `--since`.** A `--since <Nm|Nh|Nd>` global flag **must** restrict the
  query to a trailing window of `N` minutes (`m`), hours (`h`), or days (`d`). A
  malformed `--since` value **must** be rejected as a real error (FR-17) with a
  message naming the accepted forms.

### Presentation and robustness

- **FR-16 — Corrected timestamps.** The `timestamp` column in every relation and
  every command's output **must** render as the real wall-clock date/time of the
  event. otelq **must** correct the nanosecond-in-millisecond-column value
  surfaced by the reader extension (see
  [ADR-006](../adr/ADR-006-read-otlp-extension-quirks.md)); a raw 2026 event
  **must not** render as a far-future year.
- **FR-17 — Exit codes.** otelq **must** exit `0` on success, including when a
  command produces zero result rows or prints a friendly "no telemetry" message
  (FR-18, FR-19). A non-zero exit **must** occur only on a real error — e.g.
  malformed SQL (FR-9) or a malformed `--since`/argument-order parse failure
  (FR-11, FR-15).
- **FR-18 — Friendly empty-telemetry message.** When a command's required
  signal(s) are entirely absent (nothing captured), otelq **must** print a
  short, friendly message to **stderr** (pointing at the Collector / export
  toggle) and exit `0`. It **must not** surface a reader/DuckDB stack trace.
- **FR-19 — Name the gap, don't blame the Collector.** When a command's required
  signal is absent **but other signals are present**, otelq **must** print a
  message that names the missing signal (and its likely cause: the apps aren't
  emitting it, or its file was deleted under the running Collector) rather than
  the generic "is the Collector running?" text. `errors` (which needs `traces`
  or `logs`) **must** name "traces or logs" when only `metrics` is present.
- **FR-20 — Oversized export batch is skipped.** A single export batch whose
  record count exceeds the reader's 2048-row limit **must** be skipped, with a
  warning printed to stderr, rather than crashing the run. (The limit and its
  cause are described in
  [ADR-006](../adr/ADR-006-read-otlp-extension-quirks.md); the cache path shares
  this behavior per
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-15.)
- **FR-21 — Partial trailing line is skipped.** A partially-written trailing
  JSONL line (one that does not parse) **must** be skipped, and the run **must**
  still succeed; the line is re-read once complete on a later run.

## Edge Cases & Failure Modes

- **EC-1 — Nothing captured.** No matching `*.jsonl` files exist (Collector down
  or export disabled): every command prints the friendly stderr message and exits
  `0`; no stack trace is shown. (FR-18)
- **EC-2 — One signal missing, others present.** `metrics` captured but no
  `traces`/`logs`: `errors`, `slow`, and `trace` name the missing signal instead
  of blaming the Collector; `metric`/`summary` still answer from `metrics`.
  (FR-19)
- **EC-3 — Empty result, valid query.** A well-formed query that matches no rows
  (e.g. `trace <unknown-id>`, a `logs --grep` with no hits, a `metric <name>`
  with no series) exits `0`. `trace <unknown-id>` reports that no spans were found
  for the id. The `table` format renders an empty result as `(no rows)`. (FR-17)
- **EC-4 — Malformed SQL.** `sql "SELEKT 1"` exits non-zero with an `otelq: SQL
  error:` message; it does not print a Python traceback. (FR-9, FR-17)
- **EC-5 — Global flag after subcommand.** `otelq errors --format json` is
  rejected as an unrecognized argument; the user must write
  `otelq --format json errors`. (FR-11)
- **EC-6 — Malformed `--since`.** `--since 10x` (or `--since abc`) exits non-zero
  with a message naming the accepted forms (`10m`, `2h`, `1d`). (FR-15)
- **EC-7 — Far-future timestamps avoided.** With raw records carrying nanosecond
  timestamps in a millisecond column, presented `timestamp` values render in the
  correct year, not ~58000 CE. (FR-16)
- **EC-8 — Oversized batch present.** A corpus containing one export batch of
  more than 2048 records yields a stderr warning naming the batch size and the
  limit, the batch is omitted, and the rest of the corpus is queried normally.
  (FR-20)
- **EC-9 — Truncated trailing line present.** A corpus whose final line is a
  half-written JSON object is queried as if that line were absent, with no error.
  (FR-21)
- **EC-10 — Format does not change rows.** Running the same command with
  `--format table`, `--format json`, and `--format csv` yields the same logical
  rows in the same order; only the rendering differs. (INV-3)
- **EC-11 — Sparse `summary` sub-rows.** Logs present but only at one level (e.g.
  all `INFO`), and all spans `=<1s`: `summary` still emits the full skeleton for
  each present signal — the other five log levels and the `>1s` trace bucket
  appear with count `0` (and null earliest/latest, `0` services). (FR-3)
- **EC-12 — Level from `severity_number`, not text.** Logs whose `severity_text`
  is mixed-case (`Info`) but whose `severity_number` is `9` are counted under the
  `INFO` row; a log whose `severity_number` is outside `1–24` adds an `UNSET`
  row, which is absent when no such record exists. (FR-3, FR-2)

## Acceptance Criteria

> Given/When/Then, each independently testable. Command-level criteria exercise
> the in-memory `synth_conn` fixture and the direct `cmd_*` / `format_output` /
> `main` entry points; file-level robustness criteria use the file-based
> `temp_telemetry` fixture. Hints reference `just otelq`, `just otelq-test`, and
> `tests/test_otelq.py`.

- **AC-1** (Verifies FR-1, FR-2): Given a synthetic connection with traces, logs,
  gauge, and sum metrics, when each relation is queried via `sql`, then
  `traces`, `logs`, `metrics`, `metrics_gauge`, and `metrics_sum` all resolve and
  expose the columns listed in FR-2, and `metrics` returns both `gauge` and `sum`
  `metric_type` rows.
  *Verification hint: `cmd_sql` with `SELECT * FROM <relation> LIMIT 1` per
  relation; assert column names and the `metric_type` set.*
- **AC-2** (Verifies FR-3): Given a corpus with traces of differing duration,
  logs across several levels, and metrics, when `summary` runs, then the columns
  are `signal, details, count, earliest, latest, services` in that order; traces
  yield a `>1s` and a `=<1s` row; logs yield one row for each of
  `TRACE/DEBUG/INFO/WARN/ERROR/FATAL`; metrics yield a single row with empty
  `details`; and each row's count/earliest/latest/services are scoped to that
  row's subset.
  *Verification hint: `test_summary_breakdown_rows`; assert the column order, the
  present (signal, details) pairs, and per-subset counts.*
- **AC-3** (Verifies FR-4): Given a span with `status_code == 2` and an `ERROR`
  log, when `errors` runs, then both appear in one result, each tagged as a span
  or a log, ordered newest-first by `timestamp`.
  *Verification hint: `test_errors_finds_error_span_and_log`; assert both kinds
  present and descending order.*
- **AC-4** (Verifies FR-5): Given several spans of differing durations, when
  `slow --top N` runs, then exactly the `N` longest are returned in descending
  duration order, with the duration shown in milliseconds.
  *Verification hint: `test_slow_orders_by_duration_desc` with `top=2`.*
- **AC-5** (Verifies FR-6): Given the spans of one trace with a parent/child
  relationship, when `trace <trace_id>` runs, then every span of that trace is
  returned arranged as a tree (root first, children nested by depth).
  *Verification hint: `test_trace_returns_tree_for_one_trace`.*
- **AC-6** (Verifies FR-7): Given logs across services, levels, and bodies, when
  `logs` runs with `--service`, `--level`, or `--grep`, then only matching rows
  are returned (level matching is case-insensitive), newest-first.
  *Verification hint: `test_logs_filter_by_service`, `..._by_level`,
  `..._by_grep`.*
- **AC-7** (Verifies FR-8): Given a metric with multiple data points, when
  `metric <name>` runs, then its time series is returned ordered ascending by
  `timestamp` with the FR-2 metric columns.
  *Verification hint: `test_metric_returns_time_series` for `db.pool.in_use`.*
- **AC-8** (Verifies FR-9): Given a synthetic connection, when `sql "SELECT
  count(*) AS n FROM traces"` runs, then the query's own columns and rows are
  returned verbatim.
  *Verification hint: `test_sql_passthrough`.*
- **AC-9** (Verifies FR-9, FR-17, INV-5, EC-4): Given a malformed SQL string, when
  `sql` runs, then otelq exits non-zero with an `otelq: SQL error:` message and no
  Python traceback.
  *Verification hint: invoke `cmd_sql`/`main` with `"SELEKT 1"`; assert
  `SystemExit` / non-zero and the message prefix.*
- **AC-10** (Verifies FR-10, INV-2): Given any result, when `--format json` is
  selected, then the output is a JSON array of objects keyed by the result
  columns; `--format csv` emits a header row plus CSV rows; `--format table` is
  the default human layout.
  *Verification hint: `test_format_output_json`, `test_format_output_csv`,
  `test_format_output_table_empty`.*
- **AC-11** (Verifies FR-11, EC-5): Given a subcommand, when a global flag such as
  `--format json` is placed *after* it, then argument parsing fails with an
  unrecognized-argument error; placing it *before* the subcommand succeeds.
  *Verification hint: call `build_parser().parse_args([...])` with the flag after
  the subcommand and assert `SystemExit`; assert success when before.*
- **AC-12** (Verifies FR-12): Given `--dir <path>`, when a command runs, then
  otelq reads that directory; with `--dir` omitted it reads the default telemetry
  dir.
  *Verification hint: `_run(dirpath, ...)` passes `--dir`; assert results come
  from the supplied temp corpus.*
- **AC-13** (Verifies FR-13, FR-14): Given a corpus, when `--all` and `--no-cache`
  are supplied as global flags before the subcommand, then they parse and select
  the widened / cache-bypassing query path.
  *Verification hint: routing assertions live in
  `SPEC-otelq-incremental-cache` (AC-9, AC-17); here assert the flags parse as
  globals.*
- **AC-14** (Verifies FR-15, EC-6): Given `--since 10m`, when a command runs, then
  the query is restricted to the trailing 10-minute window; given a malformed
  `--since` (e.g. `10x`), otelq exits non-zero with a message naming `10m/2h/1d`.
  *Verification hint: `_parse_since` accepts `10m/2h/1d` and raises `SystemExit`
  on `10x`; window behavior cross-checked via `test_ac9_recent_default_vs_all`.*
- **AC-15** (Verifies FR-16, EC-7): Given the real fixture whose raw timestamps
  are nanoseconds stored in a millisecond column, when any relation is queried,
  then the presented `timestamp` falls in the correct (near-present) year, not a
  far-future one.
  *Verification hint: `test_integration_timestamps_are_scaled`.*
- **AC-16** (Verifies FR-17, FR-18, INV-1, INV-4, EC-1): Given an empty telemetry
  directory, when any command runs through `main`, then a friendly message is
  printed to stderr, the process exits `0`, and no `*.jsonl` file is created or
  modified.
  *Verification hint: `cmd_summary` over an empty `connect` raises
  `NoTelemetryError`; `main` catches it, prints to stderr, returns `0`
  (`test_summary_raises_when_empty`).*
- **AC-17** (Verifies FR-19, EC-2): Given a corpus where only `metrics` is
  present, when `logs`/`errors` run, then the error names the missing signal and
  differs from the generic "no telemetry" text; when nothing is present, the
  generic text is used.
  *Verification hint: `test_require_names_missing_signal_when_others_present`,
  `test_require_keeps_generic_message_when_nothing_present`,
  `test_errors_names_gap_when_only_metrics_present`.*
- **AC-18** (Verifies FR-17, EC-3): Given a valid query with no matches (unknown
  `trace_id`, non-matching `--grep`, unknown metric name), when it runs, then the
  process exits `0`; `trace` reports no spans for the id, and `table` output for
  an empty result is `(no rows)`.
  *Verification hint: `test_trace_unknown_id_raises` (caught by `main` → exit 0);
  `test_format_output_table_empty`.*
- **AC-19** (Verifies FR-20, EC-8): Given a corpus containing one export batch of
  more than 2048 records, when otelq runs, then that batch is skipped with a
  stderr warning naming the size and the limit, and the run exits `0` returning
  the remaining rows.
  *Verification hint: `test_ac15_robust_tail_parsing` (oversized batch arm);
  assert warning on stderr and success.*
- **AC-20** (Verifies FR-21, EC-9): Given a corpus whose final JSONL line is
  truncated, when otelq runs, then the partial line is skipped and the run exits
  `0` with the complete records returned.
  *Verification hint: `test_ac15_robust_tail_parsing` (truncated-line arm).*
- **AC-21** (Verifies INV-3, EC-10): Given any command, when it is rendered as
  `table`, `json`, and `csv`, then the underlying rows (and their order) are
  identical across all three formats.
  *Verification hint: run a command, capture `(columns, rows)` once, and assert
  each `format_output` rendering reflects the same rows/order.*
- **AC-22** (Verifies INV-1): Given any otelq run over a fixture, when it
  completes (including the empty-telemetry and skip paths), then no raw `*.jsonl`
  file has been modified or deleted by otelq.
  *Verification hint: `test_ac18_raw_files_unmodified` (checksum before/after).*
- **AC-23** (Verifies FR-3, EC-11): Given logs all at `INFO` and spans all
  `=<1s`, when `summary` runs, then the `logs` rows still include
  `WARN`/`ERROR`/`FATAL`/`DEBUG`/`TRACE` at count `0`, and the `traces` `>1s` row
  appears at count `0`; the present buckets carry the real counts.
  *Verification hint: `test_summary_zero_count_skeleton`; assert all six log
  levels and both trace buckets present with the expected counts.*
- **AC-24** (Verifies FR-3, FR-2, EC-12): Given logs whose `severity_text` is
  `Info` but `severity_number` is `9`, plus one log with an out-of-range
  `severity_number`, when `summary` runs, then the mixed-case records are counted
  under `INFO` (level taken from `severity_number`) and an `UNSET` row appears
  with the out-of-range record's count; with no out-of-range record, no `UNSET`
  row appears.
  *Verification hint: `test_summary_level_from_severity_number`,
  `test_summary_unset_row_only_when_present`.*
- **AC-25** (Verifies FR-3): Given a corpus where only `metrics` is present, when
  `summary` runs, then it returns exactly the single `metrics` row (empty
  `details`) and emits no zero-count log/trace skeleton; an absent signal
  contributes no rows.
  *Verification hint: `test_summary_absent_signal_has_no_rows`.*

### Examples

- **Argument order (FR-11).** `just otelq --format json errors` succeeds;
  `just otelq errors --format json` fails with
  `otelq: error: unrecognized arguments: --format json`. Subcommand flags still
  follow the subcommand: `just otelq errors --since 10m`,
  `just otelq slow --top 5`.
- **Relations for `sql` (FR-1/FR-2).**
  `just otelq-sql "SELECT service_name, count(*) FROM traces WHERE status_code = 2 GROUP BY 1"`
  groups error spans by service; `metrics` unifies `metrics_gauge` and
  `metrics_sum`, so `SELECT DISTINCT metric_type FROM metrics` returns
  `{gauge, sum}`.
- **Friendly emptiness (FR-18 vs FR-19).** With nothing captured, every command
  prints "no telemetry captured — is the collector running …?" to stderr and
  exits 0. With only `metrics` captured, `errors` instead prints "no traces or
  logs telemetry captured (present: metrics) …", naming the gap.

## Invariants

- **INV-1** — Read-only over telemetry: otelq never modifies, creates, or deletes
  the raw telemetry files it reads. Its output is a pure function of (the
  telemetry it reads, the command, and the flags).
- **INV-2** — Output-format roles are fixed: `table` is the human-facing default;
  `json` is the machine/automation format; `csv` is the spreadsheet/interchange
  format. Choosing a format never changes which command runs.
- **INV-3** — Format independence: the rows a command returns, and their order,
  do not depend on which `--format` is chosen; only the rendering differs.
- **INV-4** — Friendly failure: absent telemetry yields a human-readable stderr
  message and exit `0`; a reader/DuckDB stack trace is never the user-facing
  result of "nothing captured" or "this signal is missing".
- **INV-5** — Exit-code discipline: exit `0` covers every success including empty
  results and the friendly "no telemetry" path; a non-zero exit is reserved for
  real errors (malformed SQL, malformed `--since`, argument-order parse failure).
