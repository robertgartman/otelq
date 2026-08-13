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
last_updated: 2026-08-13
related_documents:
  - ADR-015-wall-clock-query-window
  - PRD-otelq
  - SPEC-otelq-incremental-cache
  - SPEC-otelq-worktree-scoping
  - SPEC-otelq-await
  - ADR-013-bounded-blocking-single-invocation
  - ADR-014-span-tree-traversal-in-core-sql
  - ADR-011-worktree-telemetry-identity
  - CONTRACT-telemetry-directory
  - ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0
  - ADR-009-query-history-triage-store
  - ADR-012-exit-codes-as-public-contract
ai_summary: "otelq CLI base behavior: the query relations/columns it exposes, its subcommands (incl. history/triage over the query-history store), global flags and argument order, its exit-code contract, and its friendly read-only failure handling."
semantic_tags:
  - otelq
  - cli
  - telemetry
  - duckdb
  - traces
  - logs
  - metrics
  - observability
  - exit-codes
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
[SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md); how otelq
adopts the `duckdb-otlp` reader extension's schema natively as the relations
below is recorded in
[ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0](../adr/ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0.md).
Product intent lives in [PRD-otelq](../prd/PRD-otelq.md).

## Scope

**Covered:** the named query relations/views and their column sets as exposed to
`sql` and the built-in commands; the behavior, inputs, and output columns of the
seven subcommands (`summary`, `errors`, `slow`, `trace`, `logs`, `metric`,
`sql`); the global flags (`--format`, `--dir`, `--all`, `--no-cache`, `--since`,
`--verbose`, `--version`) and the rule that global flags precede the subcommand;
the per-command output row bound (`--top`); the five output formats and their
format-independence; the `sql` filesystem-access boundary and the external-access
lockdown for built-in commands; timestamp correction (and far-future clamping) in
the presented output; the response header printed before the six signal-bearing
commands' results, naming the command, format, signal, and UTC time range; the
UTC convention for `timestamp` literals a caller writes into `sql`; and
otelq's exit-code and stderr behavior when telemetry is absent, partial, or
malformed.

**Not covered:** the raw telemetry directory and OTLP JSONL schema (an external
input — see [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md));
the parquet cache mechanics, sealing, eviction, and cache-first read routing (see
[SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md)); the
`duckdb-otlp` extension itself and the rationale for adopting its schema (see
[ADR-010](../adr/ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0.md)); and the OTel Collector
configuration that produces the raw files.

### Definitions

- **Relation / view** — a queryable table or view name exposed to SQL:
  `traces`, `logs`, `metrics`, `metrics_gauge`, `metrics_sum`,
  `metrics_histogram`, `metrics_exp_histogram`.
- **Signal** — a user-facing telemetry kind: `traces`, `logs`, `metrics`.
- **Subcommand / command** — one of the seven verbs otelq accepts
  (`summary`, `errors`, `slow`, `trace`, `logs`, `metric`, `sql`).
- **Global flag** — a flag accepted before the subcommand (`--format`, `--dir`,
  `--all`, `--no-cache`, `--since`, `--regex`, `--verbose`, `--version`);
  contrast subcommand-specific flags (`--top`, `--service`, `--level`,
  `--grep`, the `trace_id`/`name`/`query` positionals), which follow the
  subcommand.
- **Default telemetry dir** — the `.telemetry/` directory under the current
  working directory (`<cwd>/.telemetry`, per
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)).
  A cwd-relative default works both for `uv run otelq.py` from a checkout (run
  from the repo root) and for an installed copy (`uvx`/`pipx`) run from a project
  directory; a script-relative default would resolve into the install location
  (e.g. site-packages). `--dir` is therefore **never required** merely because
  otelq was installed or run through `uvx`.
- **Store resolution** — how otelq decides which telemetry directory to read when
  `--dir` is not given (FR-45). The **resolved dir** is the directory chosen; the
  **resolution mechanism** is which rule chose it. Both are disclosed (FR-29).
- **Linked worktree** — a `git worktree` checkout that is not the repository's
  main checkout. Its telemetry is written to the main checkout's store, because
  one host runs one Collector on fixed ports
  ([ADR-011](../adr/ADR-011-worktree-telemetry-identity.md)).
- **Event-time** — a record's own timestamp: the value of its relation's
  event-time column (`start_time_unix_nano` for `traces`, `time_unix_nano`
  for `logs` and the metric relations), rendered in command output as the
  presented `timestamp` column (see FR-16).
- **Result** — the `(columns, rows)` pair a command produces, rendered by the
  selected output format.
- **Response header** — a fixed-format plain-text preamble that otelq prints to
  stdout before the rendered result of the six signal-bearing commands (see
  FR-29), naming the invoked command, the resolved format, the OpenTelemetry
  signal(s) involved, the absolute telemetry directory being read **and the rule
  that chose it**, the returned rows' time range, and the session id, so an LLM
  consumer cannot mistake a rendered `timestamp` for local time or lose track of
  the result's data source.
- **Session id** — an id (see FR-33) that tags the consecutive invocations of
  one investigation so they can be correlated: a caller-supplied `--session-id`,
  or, when omitted, a freshly generated UUIDv7. Echoed verbatim in the response
  header's `Session:` line and in the stderr session footer.
- **Session footer** — a one-line stderr guidance notice (see FR-33) printed
  after every command's answer, naming the resolved session id and reminding the
  caller to reuse it via `--session-id` on related follow-up invocations.

## Functional Requirements

### Query relations and columns

- **FR-1 — Exposed relations.** otelq **must** expose exactly these query
  relations over the captured telemetry: `traces`, `logs`, `metrics`,
  `metrics_gauge`, `metrics_sum`, `metrics_histogram`, and
  `metrics_exp_histogram`. `metrics` **must** be the union of whichever of the
  four per-type metric relations are present. All seven **must** be queryable by
  the `sql` command; the built-in commands query the subset they need.
  **Expose-empty:** **all seven** relations **must** always resolve — a signal or
  metric type with no captured rows resolves to an empty (0-row) result, **not** a
  "table does not exist" catalog error. This holds for a metrics-only corpus
  (`sql "SELECT * FROM traces"` → 0 rows) **and** for a valid-but-empty or absent
  `--dir` (where the schema is probed from an embedded sample), so a "table does
  not exist" error is never surfaced. The set of relations and the empty-vs-error
  outcome **must** be identical with the cache and with `--no-cache`. Presence for
  the built-in commands is judged by **row count**, not by relation existence, so
  an all-empty corpus still takes the friendly empty-telemetry path (FR-18) and
  emits no zero-count skeleton (FR-3). The OTel **Summary** metric type is **not**
  supported (the `duckdb-otlp` reader for it is an unimplemented stub) and is
  never exposed.
- **FR-2 — Relation columns.** The six stored relations **must** carry the
  `duckdb-otlp` v0.6.0 reader columns **verbatim** — names, types, and units as
  published by the upstream extension (see
  [ADR-010](../adr/ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0.md)); the upstream
  project's documentation is the semantic reference for these columns. The
  `sql` cheat-sheet subset each relation **must** present:
  - **`traces`**: `start_time_unix_nano` (`TIMESTAMP_NS` event-time),
    `duration_time_unix_nano` (integer **nanoseconds**), `trace_id`, `span_id`,
    `parent_span_id`, `service_name`, `name` (the span name), `kind`,
    `status_code` (`0`=unset, `1`=ok, `2`=error), `status_status_message`.
  - **`logs`**: `time_unix_nano` (`TIMESTAMP_NS` event-time), `trace_id`,
    `service_name`, `severity_text`, `severity_number`, `body`.
    `severity_number` is the OTel numeric severity; otelq maps it to a
    canonical level for `summary` (FR-3) using the standard ranges **TRACE**
    `1–4`, **DEBUG** `5–8`, **INFO** `9–12`, **WARN** `13–16`, **ERROR**
    `17–20`, **FATAL** `21–24` (values outside `1–24`, including `0` and null,
    are **UNSET**). The level is derived from `severity_number`, not the
    free-form `severity_text`, which carries inconsistent casing in practice
    (e.g. `Info`).
  - **`metrics`** (the otelq-defined union view over the per-type relations):
    `time_unix_nano`, `service_name`, `name`, `metric_type`, `value`, `unit`.
    `metric_type` **must** be one of `gauge`, `sum`, `histogram`, or
    `exp_histogram`, naming the per-type relation each row originates from
    (`metrics_gauge`, `metrics_sum`, `metrics_histogram`,
    `metrics_exp_histogram`). The unified `value` **must** be, for
    `gauge`/`sum` rows, the row's scalar reading — its `double_value` when
    set, else its `int_value` cast to DOUBLE (the reader splits the OTLP
    number into the two typed columns) — and, for
    `histogram`/`exp_histogram` rows, the row's distribution `sum` (the
    histogram types have no scalar reading).
  - **`metrics_gauge`** / **`metrics_sum`**: at least `time_unix_nano`,
    `service_name`, `name`, `unit`, and the split scalar reading
    `int_value`/`double_value` (`metrics_sum` additionally carries
    `aggregation_temporality`, `is_monotonic`).
  - **`metrics_histogram`**: at least `time_unix_nano`, `service_name`,
    `name`, `unit`, `count`, `sum`, `min`, `max`, `bucket_counts`
    (`BIGINT[]`), `explicit_bounds` (`DOUBLE[]`), and
    `aggregation_temporality`.
  - **`metrics_exp_histogram`**: at least `time_unix_nano`, `service_name`,
    `name`, `unit`, `count`, `sum`, `min`, `max`, `scale`, `zero_count`,
    `zero_threshold`, the positive/negative bucket offsets and counts (typed
    `BIGINT[]` lists), and `aggregation_temporality`.

  The precise field semantics of the underlying raw records are owned by
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md);
  this requirement fixes only the column names otelq surfaces and the
  enumerations it relies on.

- **FR-43 — Span-tree relations.** otelq **must** additionally expose two
  **derived** relations that make the parent/child structure of `traces`
  traversable without the caller knowing the reader schema's conventions
  ([ADR-014](../adr/ADR-014-span-tree-traversal-in-core-sql.md)):

  - **`span_edges`** — one row per **resolvable** parent/child edge:
    `span_id`, `parent_span_id`, `trace_id`. An edge **must** be included
    **only** when the referenced parent exists **in the same trace**. A
    `parent_span_id` that is empty, or that names a span not present in the
    store, yields no row — such references are routine (the parent was sampled
    away, is still in flight, or belongs to a service that does not export) and
    **must not** be treated as an error.
  - **`span_tree`** — one row per span: `span_id`, `trace_id`, `root_id`,
    `depth`, `is_orphan`.
    - `root_id` **must** identify the **connected component** the span belongs
      to: the id of the span reached by following `span_edges` upward until no
      further edge exists. A span that is its own component's top has
      `root_id = span_id` and `depth = 0`.
    - `depth` **must** be the number of edges from the span to its `root_id`.
    - `is_orphan` **must** be true when the span names a `parent_span_id` that
      does not resolve within its trace, and false for a genuine root (empty
      `parent_span_id`). Both are component tops; only the orphan indicates a
      **partial export or a broken link**, and conflating them would hide
      exactly the fault a caller is looking for.

  Component membership **must not** span trace ids: two spans in different
  traces **must never** share a `root_id`, even if a `parent_span_id` value
  coincidentally matches.

  These relations **must** resolve identically on the cached and `--no-cache`
  paths, and **must** be queryable by `sql` like any other relation. Under
  expose-empty (FR-1) they **must** resolve (empty) rather than raise when
  `traces` has no data.

  Being **otelq-defined** rather than part of the upstream reader schema, they
  **must** be documented as such in `--help` (the `sql` views section), together
  with a worked example.

- **FR-44 — Structure is exposed as relations, not verdicts.** otelq **must**
  expose span structure as queryable relations and **must not** introduce a
  command, flag, or output field that classifies a structure as pass/fail,
  healthy/unhealthy, or connected/disconnected.

  Whether a given shape is acceptable is a policy the caller owns: which spans
  are expected, whether a missing member is fatal, and how a split trace should
  be reported are all properties of the caller's system, not of telemetry. otelq
  answers *how is this trace shaped*; the caller composes the predicate in `sql`
  and gates it through the exit-code contract of
  [ADR-012](../adr/ADR-012-exit-codes-as-public-contract.md) — optionally under
  `await` ([SPEC-otelq-await](SPEC-otelq-await.md)) when the answer may not have
  arrived yet.

  To keep that composition discoverable rather than merely possible, `--help`
  **must** carry a worked example distinguishing the two structural faults a
  caller most often needs to tell apart: a member **never observed at all**
  versus every member observed but **split across trace ids** (a hop that lost
  trace context). These have different causes and different fixes.

### The seven commands

- **FR-3 — `summary`.** `summary` **must** report a per-signal breakdown whose
  columns are, in order, `signal`, `details`, `count`, earliest `timestamp`,
  latest `timestamp`, and the distinct `service_name` count. Every numeric/time
  column **must** be scoped to its own row's subset (the count, span, and
  service count of just those records). Rows are produced **only for present
  signals**, as follows:
  - **`traces`** (when present): exactly two rows that partition spans by
    duration — `details = ">1s"` for `duration_time_unix_nano > 1s` and
    `details = "=<1s"` for the remainder. **Both** rows **must** appear even when
    a bucket's count is zero.
  - **`logs`** (when present): one row per canonical severity level
    (`TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR`, `FATAL`), the level derived from
    `severity_number` per the ranges in FR-2. **All six** rows **must** appear
    even at zero count. Log records whose `severity_number` is outside the
    canonical ranges **must** contribute an additional `details = "UNSET"` row,
    shown **only** when its count is non-zero.
  - **`metrics`** (when present): one row per metric type, with `details` set to
    the type — `gauge`, `sum`, `histogram`, `exp_histogram`. **All four** rows
    **must** appear even when a type's count is zero (a fixed skeleton like the
    log levels), and each row's count/earliest/latest/services **must** be scoped
    to that type.

  A signal with **no captured data** contributes **no rows** (its zero-count
  skeleton is not emitted) — "present" here means the signal **has captured rows**,
  not merely that its relation resolves: under expose-empty (FR-1) an absent
  signal's relation still resolves empty, yet **must not** emit a skeleton (e.g.
  metrics-only telemetry yields the four metric rows and **no** trace/log
  buckets). When **no** signal has any data at all, the friendly empty-telemetry
  behavior applies (FR-18). The zero-count rule thus governs *sub-rows within a
  signal that has data* (e.g. an `ERROR` level with no records), not signals
  without data.

  **Service list (second block).** `summary`'s output **must** consist of two
  blocks. The first is the per-signal breakdown above, rendered as the single
  FR-10 object/array (unchanged — the per-signal `services` column keeps its
  per-row distinct-service count). After it, `summary` **must** print, in
  **every** `--format`, a second block: a plain-text delimiter line
  `** List of services in telemetry data **` followed by a `service`/`count`
  table rendered in the same `--format`, one row per distinct `service_name`
  with `count` being that service's total record count across **all** signals
  (`traces` ∪ `logs` ∪ `metrics`), ordered by `count` descending (then
  `service_name` ascending for determinism), so a caller sees which services
  dominate the window and are worth zooming in on. The delimiter line makes the
  two blocks unambiguous: a machine consumer parses the first object/array
  (before the delimiter) and, if it wants the service list, the second
  (after it) — this is the one command whose stdout carries two format-rendered
  blocks, so each block individually honors the FR-10 shape rather than the
  whole stdout being one object. The second block is derived from the same
  windowed relations and is **independent** of any `--regex` filter applied to
  the first block's rows (FR-32); a record with no `service_name` contributes
  its raw (null) value as its own group.
- **FR-4 — `errors`.** `errors` **must** return error-status spans
  (`traces` rows with `status_code == 2`) and error/fatal logs (`logs` rows with
  `severity_text` in `{ERROR, FATAL}`, matched **case-insensitively** since
  `severity_text` carries inconsistent casing in practice — see FR-2), combined
  into one result and ordered newest-first by event-time. Each row **must**
  identify whether it is a span or a log, and **must** carry its record's
  `trace_id`, so a caller pivots straight to `trace <trace_id>` (FR-6) without
  a second lookup — `errors` is the triage entry point of an investigation and
  the trace tree is its localization step, so the pivot key travels with the
  row (as it already does for `slow` and `logs`). A log record with no trace
  context carries its raw (empty) `trace_id` value.
- **FR-5 — `slow`.** `slow` **must** return spans ordered by
  `duration_time_unix_nano` descending, limited to the top `N` where `N` is the
  value of `--top`
  (default **20**). The presented duration **must** be expressed in milliseconds.
- **FR-6 — `trace <trace_id>`.** `trace` **must** take a `trace_id` positional
  argument and return every span of that trace arranged as a parent/child tree
  (each span ordered under its parent by event-time, with a depth indicator). A
  span whose `parent_span_id` is absent or not present among the trace's own
  spans **must** be treated as a root. The `trace_id` argument **must** accept a
  unique **prefix** of a trace id in addition to a full id: an exact match wins;
  otherwise a prefix that matches exactly one trace id resolves to it, and a
  prefix matching two or more **must** be rejected as a real error (FR-17) naming
  the ambiguity. A prefix matching none takes the normal empty-result path (EC-3).
- **FR-7 — `logs`.** `logs` **must** return log records ordered newest-first by
  event-time, filtered by the optional subcommand flags `--service`
  (exact `service_name`), `--level` (exact `severity_text`, case-insensitive
  input), and `--grep` (case-insensitive substring of `body`). With no filter
  flags it **must** return all in-window log records.
- **FR-8 — `metric <name>`.** `metric` **must** take a `name` positional
  argument and return the time series for that metric (`metrics` rows whose
  `name` equals the given name) ordered ascending by event-time.
- **FR-9 — `sql "<query>"`.** `sql` **must** take a SQL string positional
  argument, execute it against the exposed relations (FR-1), and return its
  columns and rows. A SQL execution error **must** be reported as a real error
  (FR-17), not swallowed.
- **FR-31 — `sql` schema discoverability.** The FR-2 column list is a curated
  subset — the built-in-command contract, not the full raw schema each
  relation actually carries (e.g. `*_attributes` columns holding whatever
  custom OTel resource/scope/span/log/metric attributes an app emits, and
  `events_json`/`links_json`/`exemplars_json`). Since `sql` runs arbitrary
  DuckDB SQL (FR-9), standard introspection (`DESCRIBE <relation>`, `PRAGMA
  table_info('<relation>')`, `information_schema.columns`) already works and
  reveals this full schema without any otelq-specific support. otelq's
  `--help` and its accompanying agent skill **must** document this escape
  hatch (e.g. `sql "DESCRIBE traces"`) rather than statically enumerate every
  possible attribute — a live introspection query stays accurate as an app's
  emitted attributes change; a static list would drift.

### Global flags and argument order

- **FR-10 — `--format`.** A `--format` global flag **must** accept exactly
  `table`, `json`, `jsonl`, `csv`, or `compact`, defaulting to `compact` (otelq's
  primary consumer is an AI agent, per [PRD-otelq](../prd/PRD-otelq.md); a human
  reading the terminal opts in with `--format table`), and **must** select the
  rendering of the result. `table` is for human reading;
  `json` is a single compact JSON array for programmatic consumption (compact
  separators, no insignificant whitespace, to minimize tokens for the AI-agent
  consumer per [PRD-otelq](../prd/PRD-otelq.md)); `jsonl` emits one compact JSON
  object per line for streaming/line-oriented consumers; `csv` is the
  spreadsheet/interchange format; `compact` is a single compact JSON object of
  the form `{"columns":[...],"rows":[[...]]}` that declares the column names once
  and carries each row as a positional array — losslessly the same data as `json`
  but without repeating the column keys on every row, further reducing tokens for
  the AI-agent consumer. A `compact` result **must** be reconstructible to the
  exact records `json` would emit by zipping each `rows` entry with `columns`.
  For `summary`, `errors`, `slow`, `trace`, `logs`, and `metric`, this rendering
  is the **payload** that follows the response header of FR-29 on stdout; FR-10
  governs that payload, not the header itself.
- **FR-11 — Global flags precede the subcommand.** `--format`, `--dir`,
  `--all`, `--no-cache`, `--since`, `--regex`, `--resource-attr` (FR-40),
  `--attr` (FR-42), and `--session-id` are global
  flags and **must** be accepted *before* the subcommand. Supplying a global flag *after* the
  subcommand **must** be rejected as an unrecognized argument (a hard parse
  error), not silently accepted. Subcommand-specific flags and positionals
  continue to follow the subcommand.
- **FR-12 — `--dir`.** A `--dir <path>` global flag **must** select the
  telemetry directory to read, overriding every other rule. When omitted, otelq
  **must** resolve the directory itself per FR-45. `--dir` **must never** be
  required merely because otelq is installed or invoked via `uvx`/`pipx`: the
  default is cwd-relative, not script-relative (see Definitions).
- **FR-13 — `--all`.** An `--all` global flag **must** widen the query to the
  full raw history. (The routing this triggers is specified in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-9.)
- **FR-14 — `--no-cache`.** A `--no-cache` global flag **must** force a pure
  raw-only scan of the raw files that neither reads nor writes any cache. (Cache
  interaction is specified in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-17.)
- **FR-15 — `--since`.** A `--since <Ns|Nm|Nh|Nd>` global flag **must** restrict
  the query to a trailing window of `N` seconds (`s`), minutes (`m`), hours (`h`),
  or days (`d`). A malformed `--since` value **must** be rejected as a real error
  (FR-17) with a message naming the accepted forms.
  - The window's lower bound **must** be measured from **host wall-clock**:
    `--since 10m` means the last ten minutes, not the ten minutes preceding the
    newest record ([ADR-015](../adr/ADR-015-wall-clock-query-window.md), INV-7 of
    [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md)). The same
    basis governs the default trailing window.
  - The upper bound **must** remain `wall-clock + tolerance`, **not** wall-clock
    itself: a producer whose clock runs seconds-to-minutes ahead **must not** be
    silently dropped from results. Only the lower bound carries the caller's
    request; the upper bound exists solely to exclude implausible timestamps.
  - Wall-clock **must** be captured **once per invocation** and applied
    identically on the cached and `--no-cache` paths (FR-11).

### Presentation and robustness

- **FR-16 — Corrected timestamps.** Event-times are stored as native
  `TIMESTAMP_NS` values in the relations' `*_unix_nano` columns (FR-2), and the
  `timestamp` column every command's output presents **must** render as the
  real wall-clock date/time of the event (see
  [ADR-010](../adr/ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0.md)); a raw 2026 event
  **must not** render as a far-future year. A single implausible far-future
  event-time (a clock-skewed producer or a unit mistake) **must not** blank out
  otherwise-valid queries: the window's upper bound is a plausible ceiling
  (`wall-clock + tolerance`) applied identically on the cache and `--no-cache`
  paths, so a bogus record beyond the ceiling is excluded from a windowed result.
  Since [ADR-015](../adr/ADR-015-wall-clock-query-window.md) such a record can no
  longer push the window past real data either, because the window is not derived
  from record timestamps at all — the ceiling now only excludes the outlier. The clamp is defined once, as
  the window/watermark anchor, in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) (INV-7, EC-12);
  `doctor` surfaces the condition as a non-fatal warning (FR-26). Every rendered
  timestamp value — every command output's `timestamp` column, `summary`'s
  `earliest`/
  `latest` columns, and the FR-29 response header's `Rows time range` — **must** be an
  explicit-UTC ISO-8601/RFC-3339 string carrying a trailing `Z`, at fixed
  millisecond precision (`YYYY-MM-DDTHH:MM:SS.fffZ`, exactly 3 fractional
  digits — a fixed presentation precision; the stored values are ns-exact,
  FR-2), in every
  `--format`, so the value itself asserts UTC. A naive `str(datetime)`
  rendering (a space separator and no offset/designator) **must not** be used:
  it is visually indistinguishable from any other timezone, which would leave
  FR-29's "all timestamps are UTC" notice unverifiable from the data itself.
- **FR-30 — `sql` timestamp-literal input convention.** The relations'
  `*_unix_nano` event-time columns (FR-2) are naive (timezone-free)
  `TIMESTAMP_NS` values that always represent UTC
  wall-clock (FR-16); this is the counterpart requirement for the one place a
  caller writes a timestamp back **into** otelq, `sql`'s free-form `WHERE`
  clauses (FR-9). A caller **must** write a timestamp literal either bare
  (`'YYYY-MM-DD HH:MM:SS[.ffffff]'`) or as an ISO-8601 string with a trailing
  `Z` (e.g. `'2026-07-01T10:00:00Z'`) — both **must** be treated as UTC and
  compare correctly against the column. A literal carrying an explicit non-`Z`
  offset (e.g. `'2026-07-01T12:00:00+02:00'`) **must not** be used: the
  underlying DuckDB TIMESTAMP cast silently **discards** the offset rather than
  converting it, so the literal is compared as if its wall-clock digits were
  already UTC — a wrong comparison with no error, not a rejection. otelq
  **cannot** correct this by rewriting the caller's SQL text (`sql` is
  arbitrary, unparsed free-form SQL, FR-9/FR-27) — the convention is enforced
  only by documentation. otelq's `--help` **must** state it in an early,
  prominent block (ahead of the "argument order" section), not only inside the
  `sql views` cheat-sheet, since an agent may act on a truncated read of long
  help text. The convention is pinned by a test against otelq's actual
  relations, so a future DuckDB upgrade that changes
  this literal-parsing behavior is caught rather than silently masked.
- **FR-17 — Exit codes.** *(Amended 2026-08-12 by
  [ADR-012](../adr/ADR-012-exit-codes-as-public-contract.md), which makes the exit
  code a public compatibility surface.)* otelq's exit code **must** carry a
  three-tier taxonomy governed by one invariant: **exit `0` promises that stdout
  is the answer to the question that was asked.**

  | Exit | Meaning |
  |------|---------|
  | `0` | otelq produced an answer, and any verdict it carries is affirmative. |
  | `1` | otelq produced an answer, and the verdict is **negative**. |
  | `2` | otelq produced **no answer**. |

  Exit `0` **must** cover: a command that produces zero result rows; the friendly
  no-telemetry and named-gap paths (FR-18, FR-19); and help that was *explicitly*
  requested (`otelq help`, `otelq help <valid topic>`, `-h`/`--help`, FR-22).

  Exit `1` is the **verdict** tier: the command ran and answered a yes/no
  question in the negative. `doctor` **must** continue to exit `1` when its
  telemetry-contract validation fails (FR-26) — a store diagnosed as unhealthy is
  an *answer*, not a failure to answer. No path that merely failed to produce an
  answer may use `1`; in particular malformed SQL — which formerly exited `1` —
  **must** now exit `2`.

  Exit `2` **must** cover every case in which no answer was produced: a
  malformed `--since` or argument-order parse failure (FR-11, FR-15), an unknown
  subcommand or flag, an unknown `help` topic (FR-22), a bare invocation
  (FR-39), a telemetry directory that does not exist (FR-38), a store that
  cannot be read, malformed SQL (FR-9), and any otherwise-unhandled internal
  error. Every exit-`2` path **must** additionally emit a reason token per FR-37.

  stdout **must** carry only the answer. On any exit-`2` path stdout **must** be
  empty; diagnostics go to stderr (FR-37).
- **FR-18 — Friendly empty-telemetry message.** When a command's required
  signal(s) carry **no captured data** — and **no** other signal does either
  (nothing captured at all) — otelq **must** print a short, friendly message to
  **stderr** (pointing at the Collector / export toggle) and exit `0`. It **must
  not** surface a reader/DuckDB stack trace. "Has data" (row count), not mere
  relation existence, governs this: under expose-empty (FR-1) a required signal's
  relation may resolve empty, which **must** still trigger the friendly path (or
  the gap message of FR-19 when another signal does have data).
  - **Records outside the query window are not "nothing captured".** Since the
    window is measured from wall-clock (FR-15,
    [ADR-015](../adr/ADR-015-wall-clock-query-window.md)), a required signal can
    resolve empty simply because every record predates the window. **No** command
    **may** take the friendly path in that case — the store demonstrably holds
    that signal, so the message would be false, and false in the one case the
    caller most needs the truth. Such a command **must** answer normally: exit
    `0` with the FR-29 header, its `Query window:` line (FR-46), and zero rows.
    For `summary` this additionally means the FR-47 freshness block is emitted;
    suppressing the answer would hide the very values that explain the emptiness.
    "Nothing was captured" and "nothing was captured *recently*" are different
    diagnoses and **must not** render identically.
- **FR-19 — Name the gap, don't blame the Collector.** When a command's required
  signal has **no captured rows** **but another signal does have data**, otelq
  **must** print a message that names the missing signal (and its likely cause:
  the apps aren't emitting it, or its file was deleted under the running
  Collector) rather than the generic "is the Collector running?" text. `errors`
  (which needs `traces` or `logs`) **must** name "traces or logs" when only
  `metrics` has data. A required signal whose relation resolves empty (FR-1) is
  treated as absent here — presence is by row count, not table existence.
- **FR-20 — Export batches of any size are read.** An export batch **must** be
  read in full regardless of its record count; no size-based skip or warning is
  permitted. (Revised 2026-07-07: the former reader 2048-row limit and its
  skip-with-warning behavior are obsolete — see
  [ADR-010](../adr/ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0.md); the cache path shares
  this behavior per
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-15.)
- **FR-21 — Partial trailing line is skipped.** A partially-written trailing
  JSONL line (one that does not parse) **must** be skipped, and the run **must**
  still succeed; the line is re-read once complete on a later run.
- **FR-22 — Help affordances.** *(Amended 2026-08-12 by
  [ADR-012](../adr/ADR-012-exit-codes-as-public-contract.md): help is an answer
  only when help was the question. A bare `otelq` formerly printed help to stdout
  and exited `0`; it is now governed by FR-39.)* Beyond the seven query verbs,
  otelq **must** keep its help discoverable. otelq **must** accept a `help`
  meta-command: `otelq help` prints the top-level help and `otelq help <command>`
  prints the named command's own help (equivalent to `otelq <command> -h`). These,
  and the `-h`/`--help` flags (top-level and per-subcommand), are *explicitly
  requested* help: they **must** print to **stdout** and exit `0`. An unknown
  topic (`otelq help <unknown>`) **must** be rejected as a real error carrying
  argparse's invalid-choice message that names the valid commands — stderr, exit
  `2` (FR-17), never a silent fallback to general help.

### Output bounds, metadata, and safety

- **FR-23 — Output row bound (`--top`).** `errors`, `logs`, and `metric` **must**
  each accept a `--top N` subcommand flag that caps the number of returned rows,
  defaulting to a sane bound (**50**) so a chatty window cannot flood an agent's
  context (`slow` keeps its own default of **20**, FR-5). `N` **must** be a
  non-negative integer; a negative value **must** be rejected as a real error
  (FR-17). `--top 0` returns zero rows. When — and **only** when — the bound
  actually truncates the result, otelq **must** print a one-line notice to
  **stderr** (never stdout, so `json`/`jsonl`/`csv` stay machine-parseable). The
  cap is applied after the command's own ordering, so the retained rows are the
  first `N` of the fully-ordered result.
- **FR-32 — `--regex` result filtering.** A `--regex <pattern>` global flag
  **must** filter the result of `summary`, `errors`, `slow`, `trace`, `logs`,
  and `metric` (the FR-29 header-bearing commands; **not** `sql` — already
  supports regex natively via DuckDB's `~`/`regexp_matches` — nor
  `collector-config`/`doctor`/`troubleshoot`, which are not telemetry query
  results) to only the rows where `pattern` matches at least one of the row's
  own cell values, applied **before** the FR-10 rendering so formatting
  artifacts (JSON escaping, CSV quoting, table padding) **must not** affect
  match precision. `pattern` **must** be a standard Python `re` pattern,
  matched case-sensitively via `re.search` against each cell's string form
  (`None` cells excluded) — a caller wanting case-insensitive matching uses the
  inline `(?i)` flag. A malformed pattern **must** be rejected as a real error
  (FR-17) naming the underlying `re` error, not a raw traceback. Supplying
  `--regex` with a command outside its supported set **must** be rejected as a
  real error naming the unsupported command, not silently ignored. The filter
  **must** apply to the same fully-ordered, already `--top`-capped result the
  command would otherwise return (FR-23) — i.e. the same rows a caller piping
  otelq's rendered output through `grep` would see today — not a wider,
  unbounded scan; a caller needing to search beyond the cap raises `--top`
  themselves. When `--regex` is supplied, the FR-29 response header **must**
  additionally report the verbatim pattern and how many rows it removed, so a
  caller is never blind to what was filtered away (unlike post-hoc `grep` on
  rendered output).
- **FR-33 — Session id.** A `--session-id <string>` global flag **must** tag an
  invocation with a caller-chosen id that groups the consecutive invocations of
  one investigation. When supplied, the id **must** be reported **verbatim** in
  the FR-29 response header's `Session:` line (for the six header-bearing
  commands). When **not** supplied, otelq **must** generate a fresh id per
  invocation using **UUIDv7** (RFC 9562 — a time-ordered, collision-resistant
  id), with **no** third-party dependency added (otelq's runtime dependency set
  stays `duckdb` alone). The resolved id (supplied or generated) **must** be
  identical everywhere it appears within a single invocation — the header
  `Session:` line and the session footer below. Additionally, on **every**
  command invocation (including the header-less `sql`/`doctor`/`history`/
  `collector-config`/`troubleshoot`), otelq **must** print a **session footer**
  to **stderr** — preceded by a blank line — that names the resolved id and
  instructs the caller to pass `--session-id <id>` on consecutive related
  invocations to keep the analysis correlated. The footer **must** be on stderr,
  never stdout, so it never corrupts the machine-parseable payload (mirroring
  otelq's other stderr guidance notices, and preserving `sql`'s pure-data
  stdout contract per INV-6). The footer does not alter the result rows, their
  order, or their rendering (INV-3).
- **FR-24 — `--version`.** A `--version` global flag **must** print otelq's own
  version and exit `0`. The reported version **must** match the packaged
  distribution version (so an agent can name the exact build it drives, relevant
  to the DuckDB/extension pin governance of
  [ADR-003](../adr/ADR-003-duckdb-otlp-extension-pin-governance.md)).
- **FR-25 — `--verbose`.** A `--verbose` global flag **must**, without changing
  the result rows or their rendering (INV-3), print a one-line description of the
  resolved query plan — the event-time window it covered, and how much of it was
  served from the cache versus gap-filled from raw — to **stderr**, so a result is
  self-describing and window/route surprises are diagnosable. When a `trace`
  lookup widens from an empty default window to the full history (FR-10 of
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md)), `--verbose`
  **must** also note that widening.
- **FR-26 — `doctor` cache-health and clock-skew checks.** The `doctor` command
  **must**, in addition to validating the telemetry directory against
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md),
  report non-fatal diagnostics for the cache failure modes that silently degrade
  queries: cache-directory writability (a read-only dir disables the cache),
  a stale writer lock, an incompatible cursor schema version, and a newest cached
  event-time more than the clamp tolerance ahead of wall-clock (the FR-16 / cache
  INV-7 condition). These checks **must** be reported as `OK`/`INFO`/`WARN`
  only — never `FAIL` — because queries still answer from the raw files regardless,
  and **must not** change `doctor`'s exit code, which stays governed by the
  telemetry-contract validation.
- **FR-27 — `sql` filesystem boundary and built-in lockdown.** The `sql` command
  is an ad-hoc analysis escape hatch: it executes arbitrary SQL against the
  exposed relations with the invoking user's filesystem access (it can read and
  write local files via `read_csv`, `COPY`, etc.). otelq's help **must** document
  this so a caller treats an untrusted query with the same care as a shell
  command. Every **built-in** command (all commands other than `sql`) **must**
  run with DuckDB's external filesystem/network access revoked once its query
  relations are materialized, as defense-in-depth so a crafted relation cannot
  reach other files. Revoking access **must not** change any built-in command's
  result (its relations are already built by that point).
- **FR-28 — SQL-safe telemetry paths.** otelq **must** operate correctly when the
  telemetry directory (or any path it derives) contains characters that are
  significant in SQL string literals — notably a single quote (common on macOS,
  e.g. `Robert's Mac`). Such a path **must not** cause a SQL syntax error or
  permit injection; every filesystem path spliced into SQL **must** be escaped or
  bound. This holds identically on the cache and `--no-cache` paths.
- **FR-29 — Response header.** For `summary`, `errors`, `slow`, `trace`, `logs`,
  and `metric` — **not** `sql`, `collector-config`, `troubleshoot`, or `doctor` —
  otelq **must** print a fixed-format response header to **stdout**, immediately
  before the command's rendered result, for **every** `--format` value:
  ```
  ==========
  otelq <command> response, format <format>
  OpenTelemetry signal: <signal>
  OTEL source dir: <directory>
  OTEL source resolved by: <mechanism>
  Rows time range: <from> - <to>
  Query window: <window>
  IMPORTANT: all timestamps are UTC
  Session: <session-id>
  ----------
  ```
  - `<command>` **must** be the literal invoked subcommand name.
  - `<format>` **must** be the resolved `--format` value (`table`, `json`,
    `jsonl`, `csv`, or `compact`). `json`, `jsonl`, `csv`, and `table` are
    self-describing shapes an LLM consumer already recognizes; `compact` is
    otelq-specific, so when `<format>` is `compact` the format line **must**
    append a literal shape hint: `, a {"columns":[...],"rows":[[...]]} object —
    column names once, each row a positional array`. No other format gets a
    hint appended.
  - `<signal>` **must** use the plural signal names already defined in
    Definitions (`traces`, `logs`, `metrics`): `traces` for `slow` and `trace`;
    `logs` for `logs`; `metrics` for `metric`. For `summary` and `errors`, whose
    rows can span more than one OpenTelemetry signal (FR-3, FR-4), `<signal>`
    **must** be the set of signals actually represented among the returned rows,
    comma-joined in the fixed order `traces, logs, metrics` (only the present
    ones listed), or `n/a` when the result has zero rows.
  - `<directory>` **must** be the absolute, normalized path of the telemetry
    directory actually used by the invocation. A relative default or `--dir`
    value **must** be resolved before it is shown.
  - `<from>` and `<to>` on the `Rows time range:` line **must** be the minimum
    and maximum `timestamp` value among the command's returned rows, rendered with the same corrected-UTC
    formatting used elsewhere in the output (FR-16); both **must** render as
    `n/a` when the result has zero rows.
  - The header **must** precede the FR-10 rendering of the result and **must
    not** itself be rendered as `json`/`jsonl`/`csv`/`compact` — it is always
    this fixed plain-text block, identical in structure (line count and
    labels) regardless of `--format` (only the `<format>` value, and the
    `compact`-only shape hint, vary within the format line). A consumer that
    needs the bare payload skips past the line of ten `-` characters.
  - The header **must** include a `Session: <session-id>` line (immediately
    after the `IMPORTANT` line, before the closing `----------`) carrying the
    verbatim session id resolved for the invocation (FR-33).
  - `OTEL source dir:` **must** carry the **absolute, resolved** telemetry
    directory actually read, and `OTEL source resolved by:` **must** carry the
    FR-45 mechanism that selected it. Both lines **must** be present on every
    header, in that order, immediately after the signal line. Two invocations can
    then be **proven** to have read the same store by comparing output, rather
    than by trusting that two callers implement the same path rule.
  - The header **must not** change which rows are returned, their order, or the
    FR-10 rendering rules applied to the payload that follows it (INV-6).
  - When `--regex <pattern>` (FR-32) is supplied, the header **must** insert
    two additional lines after `Query window` and before the `IMPORTANT` line:
    `Regex filter applied: <pattern>` (the verbatim pattern) and `Rows removed
    by regex: <count>` (how many rows the filter excluded). These lines
    **must not** appear when `--regex` is not supplied — the header's line
    count is otherwise fixed, but this pair is the one deliberate exception,
    mirroring the `compact`-only format-line suffix.
  - When one or more `--resource-attr` filters (FR-40) are supplied, the header
    **must** insert a single additional line after `Query window` and before the
    `IMPORTANT` line: `Resource filter applied: <k>=<v>[, <k>=<v>…]`, listing
    every filter in the order given (a presence-only filter renders as a bare
    `<k>`). The line **must not** appear when no `--resource-attr` is supplied.
    A reader **must** be able to see what was filtered without re-deriving it
    from the invocation. `--attr` (FR-42) **must** contribute an equivalent but
    **separate** `Attribute filter applied:` line, so a reader can tell a
    producer-level filter from a record-level one.
  - The row-derived line **must** be labelled `Rows time range:` and the
    window line `Query window:` (FR-46), in that order. The former describes the
    rows **returned**; the latter describes the range **searched**. The earlier
    label `Time range:` asserted the second meaning while carrying the first, so
    a consumer reading it as the queried window was silently wrong; the label is
    therefore explicit on both lines rather than inferred.
  - Under `await` ([SPEC-otelq-await](SPEC-otelq-await.md) FR-7), the header
    **must** additionally carry a `Waited: <n>ms (<p> polls)` line.

### Query history and triage

> The on-disk query-history store (`.otelq-history/`) is governed by
> [ADR-009](../adr/ADR-009-query-history-triage-store.md); these requirements
> pin the CLI behaviour over it.

- **FR-34 — `history` command.** A `history` subcommand **must** report the
  stored past-query templates ranked most-promising-first, capped by `--top`
  (default `10`). Output columns **must** be `rank`, `score`, `terminal_pct`,
  `uses`, `last_used`, `command`, `query` (the template's latest concrete
  invocation). `score` **must** equal `rf × (wf + 1) / (rf + 2)`, where
  `rf = Σ 0.5^(age / half-life)` over the template's invocations and `wf` is
  the same decayed sum restricted to invocations that **ended their session**
  with a *usable* row count (`1..max-rows` — neither zero nor a flood).
  `half-life` (default 7 days) and `max-rows` **must** be
  environment-configurable with these defaults. `last_used` **must** render as
  an explicit-UTC `Z`-suffixed string (FR-16). Over a store that does not yet
  exist, `history` **must** print a friendly stderr notice and exit `0`
  (FR-18's fail-friendly rule); over a torn/unreadable store it **must**
  return an empty report, never a traceback. Neither `history` nor `triage`
  is ever itself recorded into the store.
- **FR-35 — `triage` command.** A `triage` subcommand **must** decide, from
  the store alone, whether the caller is mid-investigation or starting fresh,
  and act:
  - **Anchor detection.** With `--session-id` supplied, the anchor is the
    latest stored invocation carrying that id (any age — the caller asserted
    continuity). Without it, triage **must** treat the call as fresh-start:
    recency **must never** anchor a chain, because concurrent agent sessions
    interleave in one store and the latest row can belong to a different
    investigation entirely.
  - **Candidates.** Mid-chain: the decay-weighted **first-order successors**
    (adjacent within a session) of the anchor's template, each weighted by its
    own FR-34 score. Fresh-start: session-**opening** templates weighted by
    the decayed weight of the successful sessions they opened.
  - **Auto-run.** The best candidate **must** be executed as a real otelq
    invocation — banner line first, then the command's normal output — **only
    when** its evidence (decayed observation weight) and share (fraction of
    observations from that anchor) clear the configured thresholds (defaults
    `2.0` / `0.5`) **and** the template is *concrete* (normalisation
    introduced no `?` placeholder). The run **must** execute under the
    anchor's session id, and **must** be recorded into history so the chain
    advances; the `triage` wrapper itself **must not** be recorded.
  - **Suggestion.** After an auto-run — or instead of one, when the winning
    candidate is confident but not concrete — triage **must** print, as the
    **last stdout line**, the full next invocation to copy
    (`otelq --dir <dir> --session-id <id> <template's latest raw form>`),
    gated by the softer suggest thresholds (defaults `1.0` / `0.4`).
  - **Grounded fallback.** With no candidate clearing any threshold: when the
    current session has **not** yet run `summary`, triage **must** auto-run
    `summary` itself — the RCA guide's grounding step — with a banner naming
    why, recorded and suggestion-checked like any auto-run (a fresh session
    trivially qualifies). Only when the session **already** contains a
    `summary` may triage refuse: it **must** say it has no strong candidate
    (stderr) and print the FR-34 ranked list (`--top` default `20`) instead of
    guessing. With no store at all it **must** print a friendly stderr notice
    directing the caller to the RCA guide and exit `0`.
  - All four thresholds **must** be environment-configurable.
- **FR-36 — Sessions are explicit session ids, and nothing else.** Only an
  **explicitly supplied** `--session-id` may be stored on a history row; a
  generated (FR-33 default) id **must** be stored as NULL — it is an offer in
  the footer, not an asserted correlation. When sessionising (FR-34/FR-35), a
  session **is** the set of rows sharing one non-null session id, ordered by
  event time (an investigation resumed hours later stays one session; two ids
  interleaved seconds apart stay two sessions). Timestamps **must never** be
  used to infer session membership — concurrent agent sessions writing to one
  store interleave in time, and a gap heuristic would chain unrelated
  investigations. A row with **no** session id belongs to **no** session: it
  **must** still count toward frequency/recency ranking evidence, and **must
  not** contribute terminal, transition, or session-opening evidence.

### Resource-attribute correlation

> An OpenTelemetry **Resource** attribute identifies the producing entity, so it
> is the correlation spine that ties one run's traces, logs, and metrics
> together. These requirements expose it generically. otelq **must not** bless,
> special-case, or document any particular attribute *name* as meaningful —
> `service.namespace`, `deployment.environment`, and any consumer-chosen key are
> all just keys. (`otelq.worktree.id` is otelq's own key, governed by
> [SPEC-otelq-worktree-scoping](SPEC-otelq-worktree-scoping.md); it is read
> through the very same mechanism, not a privileged path.)

- **FR-40 — `--resource-attr` correlation filter.** A `--resource-attr` global
  flag (FR-11) **must** filter results by an OTel **Resource** attribute, in two
  forms:

  | Form | Meaning |
  |------|---------|
  | `--resource-attr <key>=<value>` | The attribute `key` is present and its value is **exactly** `value` |
  | `--resource-attr <key>` | The attribute `key` is present with any non-empty value |

  Rules:
  - The flag **must** be **repeatable**, and repeats **must** combine
    **conjunctively** (AND). Order does not affect the result set.
  - Matching **must** be **exact string equality**. Substring, prefix, and
    case-insensitive matching **must not** be offered as the default or implied
    by it — a value that is a strict substring of another row's value **must
    not** match it. (A run id that prefixes another run's id must never select
    the wrong run.)
  - A `key` **must** be usable verbatim, with no caller-side escaping or
    quoting, including the dotted keys OTel conventionally uses
    (`service.namespace`, `smoke.run_id`). A dotted key **must** address the
    literal flat attribute of that name, **never** a nested path traversal.
  - The flag **must** apply identically to all six signal-bearing commands —
    `summary`, `errors`, `slow`, `trace`, `logs`, `metric` — so one flag scopes
    the whole correlation spine regardless of signal. Supplying it to any other
    command (`sql`, `doctor`, `collector-config`, `troubleshoot`, `history`,
    `triage`, `set_resource_attributes`) **must** be a real error
    (`usage_error`, exit `2`, FR-37), never a silent no-op; for `sql` the message
    **must** name the FR-41 macro as the supported route.
  - A malformed argument — an empty key, or `<key>=` with an empty value —
    **must** be rejected as `usage_error` (exit `2`), not silently treated as a
    never-match. An attribute whose stored value is the empty string **must**
    read as *absent* (consistent with FR-41), which is why an empty value cannot
    be asked for.
  - The filter composes conjunctively with `--since`/`--all`, `--regex`,
    `--top`, the per-command filters (`--service`, `--level`, `--grep`), and
    with worktree scoping. It **must** be disclosed in the response header
    (FR-29).
  - A row whose `resource_attributes` are absent, empty, or unparseable
    **must** be treated as not matching, and **must not** raise (FR-41).

- **FR-41 — `resource_attr()` SQL macro.** otelq **must** define a DuckDB macro
  on the query connection:

  ```sql
  resource_attr(<attributes-column>, '<key>')
  ```

  returning the string value of Resource attribute `key`, or `NULL` when the
  attribute is absent, its value is the empty string, or the column is NULL or
  not valid JSON. It **must never** raise on a malformed value.

  This macro is the **single canonical way** to read a resource attribute
  anywhere in otelq: the FR-40 predicate, the worktree predicate and the
  ready-to-paste worktree clause
  ([SPEC-otelq-worktree-scoping](SPEC-otelq-worktree-scoping.md) FR-9/FR-13) all
  **must** be expressed through it, so one mechanism has one behavior.

  It **must** be available to `sql`, giving an ad-hoc query the same
  storage-independence FR-40 gives the built-in commands — `sql` remains
  **verbatim** and unrewritten (worktree-scoping FR-9), so the caller opts in by
  writing the macro. `sql` is otherwise the only route by which a caller could
  filter on a resource attribute, and doing so by hand requires knowing the
  column name, that it holds JSON, and which JSON function to call — knowledge
  this macro exists to remove.

  Because it is **otelq-defined** — neither an OpenTelemetry concept nor a
  DuckDB builtin — `--help` **must** document it in the `sql` views/cheat-sheet
  section, stating plainly that it is an otelq affordance and what it returns.
  Its name, arity, and return semantics are a compatibility surface: a consumer
  embeds it in stored queries, so they **must not** change incidentally.

- **FR-42 — `--attr` signal-attribute filter.** An `--attr` global flag (FR-11)
  **must** filter results by the attributes attached to the individual
  **record** — the span, log record, or metric data point — as opposed to the
  **Resource** attributes of its producer (FR-40). Its two forms, repeatability,
  conjunctive combination, exact-equality semantics, dotted-key handling,
  malformed-argument rejection, unsupported-command rejection, and header
  disclosure **must** all match FR-40 exactly, so one rule governs both filters.

  The attribute column is selected by the **signal being queried**, not by the
  caller: `span_attributes` for traces, `log_attributes` for logs,
  `metric_attributes` for metrics. A command spanning several signals
  (`summary`, `errors`) **must** apply the filter to each signal against that
  signal's own column. The caller therefore never names a column, and the same
  `--attr step=pair-node` means the same thing whichever signal answers it.

  Extraction **must** go through the FR-41 macro, which is generic over any
  JSON attributes column — record attributes are not a second mechanism.

  The distinction from FR-40 is load-bearing and **must not** be blurred: a
  Resource attribute identifies the *producer* (and so is the correlation spine
  for a whole run), while a record attribute distinguishes *one event from
  another within* that producer. Matching a record attribute against Resource
  attributes, or vice versa, would let a mis-instrumented producer read as a
  false positive.

  The header line of FR-29 **must** distinguish the two, rendering
  `Attribute filter applied: <k>=<v>[, …]` separately from FR-40's
  `Resource filter applied:` line.

- **FR-45 — Store resolution.** When `--dir` is not given, otelq **must** resolve
  the telemetry directory itself, taking the **first** rule that yields a
  directory (rules 3 and 4 additionally require that it exist; rule 2 does not —
  see below):

  | # | Rule | Reported mechanism |
  |---|------|--------------------|
  | 1 | The `--dir` flag | `--dir flag` |
  | 2 | The `OTELQ_DIR` environment variable | `OTELQ_DIR env variable` |
  | 3 | Invoked inside a **linked git worktree** ⇒ the `.telemetry/` of the repository's **main checkout** | `git main checkout (worktree '<name>', default branch '<branch>')` |
  | 4 | The nearest `.telemetry/` at, or above, the working directory | `CWD or an ancestor` |

  Rules and their rationale:

  - `--dir` **must** win unconditionally, including over `OTELQ_DIR`: an explicit
    argument is the caller's most specific statement of intent.
  - `OTELQ_DIR`, when set and non-empty, **must** be used even if it does not
    exist — in which case FR-38 reports `store_not_found` against it. A typo in a
    deliberately-set variable **must not** be silently papered over by falling
    through to discovery, because two cooperating processes sharing that variable
    would then read different stores, which is the failure this rule exists to
    prevent.
  - Rule 3 **must** take precedence over rule 4. A linked worktree may contain
    its own `.telemetry/` — often an empty one — while the telemetry it produces
    is written to the main checkout's store, because one host runs one Collector
    on fixed ports (ADR-011). Preferring the local directory would answer
    truthfully about an empty store while the data sits elsewhere: a correct
    answer to the wrong question, which the caller has no signal to detect.
  - Rule 3 **must** apply only in a **linked** worktree. In the main checkout it
    is not a redirect, and rule 4 governs.
  - Rule 4 **must** walk upward from the working directory and stop at the
    **first** `.telemetry/` found, so a nested project cannot silently answer
    from an ancestor project's store once it has one of its own.
  - Resolution **must** be read-only: it **must not** create any directory it
    inspects (FR-38, CONTRACT root ownership).
  - When git plumbing is unavailable or the directory is not a repository, rule 3
    **must** be skipped without error — discovery falls through to rule 4.

  When no rule yields a directory, otelq **must** fail per FR-38
  (`store_not_found`, exit `2`) with a message naming **every path tried**, so a
  caller can see where otelq looked rather than guessing.

  The resolved directory and the mechanism **must** be disclosed on every answer
  (FR-29) and in the machine failure object (FR-37).

### Window and freshness disclosure

- **FR-46 — The effective query window is disclosed on every answer.** The FR-29
  response header **must** carry a `Query window:` line stating the event-time
  range the invocation **actually applied** to the query relations, and how that
  window was chosen. A caller must never have to re-derive the window from the
  flags it passed, because the window is not a restatement of the flags: it is
  computed (FR-9, INV-7) and a default applies when no flag is given.
  - A bounded window **must** render as `<from> - <to> (<width>, <origin>)`,
    where `<from>`/`<to>` are the applied bounds in the corrected-UTC form of
    FR-16, `<width>` is the compact form (`30s`, `10m`, `2h`, `1d`), and
    `<origin>` is `--since` when the width came from the flag and `default` when
    it came from the built-in window.
  - An unbounded window **must** render as `all history (<origin>)`, where
    `<origin>` is `--all` when the flag was given and `command default` when the
    command is unbounded by definition (`trace`, FR-6).
  - The line **must** be present even when the result has **zero rows** — the
    case where it is the only thing distinguishing "nothing matched in the range
    I searched" from "I searched a range you did not intend". `Rows time range:`
    renders `n/a - n/a` there and is, by construction, uninformative.
  - The disclosed bounds **must** be the bounds applied to the relations, and
    **must** be identical on the cached and `--no-cache` paths for the same
    invocation (FR-11, INV-7). Disclosure that could disagree with the query it
    describes would be worse than no disclosure.
  - The bounds **must** be the wall-clock-derived bounds of FR-15
    ([ADR-015](../adr/ADR-015-wall-clock-query-window.md)): `<from>` is the
    applied lower bound and `<to>` is **wall-clock at invocation**, so the
    rendered span equals the requested width. `<to>` **must not** be the
    implausibility ceiling of FR-15 — rendering a 30-minute window as a day wide
    would misdescribe the search far more than it disclosed the guard. A record
    from a clock-skewed producer may therefore carry a `timestamp` slightly
    **after** `<to>` and still legitimately appear (FR-15, EC-12); this is
    stated here so a reader meets it as documented behaviour rather than
    inferring a defect from a suspicious timestamp.
  - A consumer **must not** infer freshness from these bounds: they read
    identically whether or not anything is arriving, which is why freshness is
    reported separately (FR-47, and
    [SPEC-otelq-await](SPEC-otelq-await.md) FR-13).
  - The line **must not** change which rows are returned or how they are
    rendered (INV-6).

- **FR-47 — `summary` reports per-signal ingestion freshness.** `summary`
  **must** append a final block naming, for each signal that has captured data,
  the newest record's event-time and that record's age relative to wall-clock, so
  a caller can distinguish **"nothing happened"** from **"the producer has not
  flushed yet"** — two conditions that are otherwise indistinguishable from an
  empty result.
  - The block **must** be delimited by the literal label
    `** Newest record per signal **`, mirroring the existing service block
    (FR-3), and carry the columns `signal`, `newest`, `age`.
  - `newest` **must** use the corrected-UTC form (FR-16); `age` **must** be the
    compact elapsed form (`4s`, `1m02s`, `3h12m`, `41d15h`) measured against
    wall-clock at invocation, degrading to coarser units as it grows so a long
    silence stays readable rather than rendering as a four-digit hour count.
  - The values **must** be computed over **all records visible to the build for
    that signal, not restricted to the query window**. Freshness answers a
    question about the producer, not about the window; scoping it to the window
    would make it tautological.
  - A signal with no captured data **must not** appear, consistent with FR-3.
  - The block **must** be emitted even when the per-signal block is empty because
    every record falls outside the window (FR-18). That is the case it exists
    for: it is what distinguishes a stalled producer from one that never ran.
  - The block **must** report a **number, never a verdict** — no threshold, no
    `STALE`/`OK` label. Which lag is acceptable is the caller's policy, and
    otelq does not hold it (FR-44).
  - This block **must** be the only place a regular query pays for freshness:
    the FR-29 header **must not** carry per-signal freshness, so header height
    stays effectively fixed and ordinary commands are not bloated by a diagnostic
    that `summary` exists to answer.


### Machine-attributable failure (ADR-012)

- **FR-37 — Reason tokens on failure.** Every exit-`2` path (FR-17) **must**
  emit, on **stderr**, exactly one line of the form
  `otelq: <reason>: <human message>`. Other stderr content (an argparse usage
  line, the full help of FR-39) **may** precede it, so a consumer matches the
  line rather than assuming it is first. The token vocabulary is **closed** and
  drawn from:

  | Reason | Emitted when |
  |--------|--------------|
  | `store_not_found` | The resolved telemetry directory does not exist (FR-38) |
  | `store_unreadable` | The path exists but is not a usable store — not a directory, or its contents cannot be read |
  | `query_error` | A query failed to execute — malformed SQL (FR-9), an empty query, or a reader/DuckDB error |
  | `usage_error` | The invocation was not understood — bad or misplaced flag, unknown subcommand, unknown `help` topic, malformed `--since` or `--regex`, an ambiguous `trace` id prefix, bare invocation (FR-39) |
  | `internal_error` | Any otherwise-unhandled error |
  | `interrupted` | A bounded wait was signalled ([SPEC-otelq-await](SPEC-otelq-await.md) FR-11) |
  | `timeout` | A bounded wait reached its deadline unsatisfied — exit `1`, a verdict (SPEC-otelq-await FR-6) |

  `predicate_unsatisfied` remains **reserved** and **must not** be emitted.

  Every token above accompanies exit `2` except `timeout`, which is the one
  token attached to a **verdict**: it accompanies exit `1` from `await`, so a
  consumer can read every non-zero outcome with one rule. No other exit-`1`
  path is required to carry a token.

  Tokens are additive-only: a future token **may** be introduced, but an
  existing token's spelling, meaning, and associated exit code **must not**
  change.

  When `--format` is a machine format (`json`, `jsonl`, `compact`), otelq **must
  additionally** print a single-line JSON object to **stderr** carrying at least
  `otelq_version`, `ok` (always `false`), `reason`, `store.dir` (the resolved
  absolute telemetry directory) and `store.resolved_by` (the FR-45 mechanism). It
  **must not** be printed for the human formats (`table`, `csv`).

  The failure object **must never** be written to **stdout**: a caller piping
  stdout to a parser must receive rows or nothing, never an error wearing the
  shape of data. On every exit-`2` path stdout **must** be empty.

- **FR-38 — A missing telemetry root is a failure, and is never created.** When
  the resolved telemetry directory (FR-45, whichever rule chose it) does not
  exist, otelq **must** exit
  `2` with reason `store_not_found`, naming the resolved absolute path. It
  **must not** create that directory, and **must not** take the friendly
  empty-telemetry path of FR-18 — a directory that is absent is *unavailable*,
  which is a different condition from a directory that is present and empty.

  otelq **must** create its own consumer-owned subtrees (`.otelq-cache/`,
  `.otelq-history/`; see
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md))
  **only** inside a root that already exists. The telemetry root itself is
  producer-owned. The cwd-relative default of FR-12 is unchanged — only the
  response to its absence is.

  This rule **must not** apply to commands that do not read a store — `--help`,
  `help`, `--version`, `collector-config`, `troubleshoot`,
  `set_resource_attributes` — nor to **`doctor`**, whose purpose is to diagnose
  the store: `doctor` **must** continue to report a missing directory as a `FAIL`
  row and exit `1` (a verdict, FR-26), not `2`.

- **FR-39 — A bare invocation is a usage error.** `otelq` invoked with **no
  arguments at all** **must** print the top-level help to **stderr** and exit `2`
  with reason `usage_error` (FR-37). It **must not** print to stdout and **must
  not** exit `0`.

  Rationale, per [ADR-012](../adr/ADR-012-exit-codes-as-public-contract.md): a
  bare invocation is the shape produced when a caller's argument variable fails to
  expand. Answering it with help on stdout and exit `0` hands an automated caller
  a success code alongside output that is not the answer to any question it
  asked. Explicitly requested help (FR-22) keeps stdout and exit `0`, because
  there the help *is* the answer.

## Edge Cases & Failure Modes

- **EC-1 — Nothing captured.** No matching `*.jsonl` files exist (Collector down
  or export disabled): every built-in command prints the friendly stderr message
  and exits `0`; no stack trace is shown. The relations still resolve empty
  (seeded from the embedded schema probe), so `sql "SELECT count(*) FROM traces"`
  returns `0` rather than a catalog error. (FR-1, FR-18)
- **EC-2 — One signal missing, others present.** `metrics` captured but no
  `traces`/`logs`: `errors`, `slow`, and `trace` name the missing signal instead
  of blaming the Collector; `metric`/`summary` still answer from `metrics`; and
  the `traces`/`logs` relations still **resolve empty** (FR-1), so
  `sql "SELECT * FROM traces"` returns 0 rows rather than a catalog error.
  (FR-1, FR-19)
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
- **EC-7 — Far-future timestamps avoided.** Presented `timestamp` values render
  in the correct (near-present) year for genuine events, never a far-future one,
  whatever encoding the reader surfaces. (FR-16)
- **EC-8 — Large batch present.** A corpus containing one export batch of
  more than 2048 records is read in full — no warning, no omission — and queried
  normally alongside the rest of the corpus.
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
- **EC-13 — Unknown help topic.** `otelq help not-a-command` **must** exit
  non-zero with argparse's invalid-choice error naming the valid commands; it
  **must not** silently fall back to general help and exit `0`. (FR-22, FR-17)
- **EC-14 — `--top` truncation notice.** Given more matching rows than the bound,
  `otelq logs --top 2` returns exactly two rows and prints a one-line truncation
  notice to **stderr**; under the bound (e.g. `--top 50` over six rows) the full
  result is returned and **no** notice is printed. The notice never appears on
  stdout. (FR-23)
- **EC-15 — `--since` seconds unit.** `--since 30s` restricts the query to the
  trailing 30 seconds of **wall-clock** (FR-15,
  [ADR-015](../adr/ADR-015-wall-clock-query-window.md)), a tighter window than the
  `1m` floor previously allowed. (FR-15)
- **EC-16 — `--version`.** `otelq --version` prints `otelq <version>` and exits
  `0`, where `<version>` equals the packaged distribution version. (FR-24)
- **EC-17 — `--verbose` metadata.** `otelq --verbose summary` prints the same
  result rows as without `--verbose`, plus a one-line window/route/cache
  description to stderr; the stdout rows and their order are unchanged. (FR-25,
  INV-3)
- **EC-18 — `jsonl` format.** `--format jsonl` emits one compact JSON object per
  line (each line independently parseable), while `--format json` emits a single
  compact array; both carry the same logical rows in the same order as `table`.
  (FR-10, INV-3)
- **EC-19 — Trace-id prefix.** `trace <unique-prefix>` returns the matching
  trace's tree; an exact id still works; a prefix matching two or more trace ids
  exits non-zero with an ambiguity message; a prefix matching none takes the
  empty-result path (EC-3). (FR-6, FR-17)
- **EC-20 — Built-in lockdown vs `sql` escape hatch.** After a built-in command's
  relations are built, DuckDB external file access is revoked, so a built-in
  cannot be coerced into reading an unrelated file; `sql` retains file access so
  `read_csv`/`COPY` continue to work as documented. (FR-27)
- **EC-21 — Quote in `--dir`.** A telemetry directory whose path contains a single
  quote (e.g. `.../Robert's Mac/.telemetry`) is queried normally — no SQL syntax
  error, identical result cached vs `--no-cache`. (FR-28)
- **EC-22 — `compact` format.** `--format compact` emits a single JSON object
  `{"columns":[...],"rows":[[...]]}` — the column names once, then one positional
  array per row — carrying the same logical rows in the same order as `table` and
  `json`. Zipping each `rows` entry with `columns` reconstructs exactly the
  objects `--format json` would emit. (FR-10, INV-2, INV-3)
- **EC-23 — Response header present/absent.** `otelq --format json summary` (and
  likewise `errors`/`slow`/`trace`/`logs`/`metric`) prints the fixed FR-29 header
  to stdout before the JSON payload; `otelq sql "..."`, `doctor`,
  `collector-config`, and `troubleshoot` print no such header. A zero-row result
  (e.g. `metric <unknown-name>`) still prints the header, with
  `Rows time range: n/a - n/a`, while `Query window:` still carries the applied
  bounds (FR-46). A corpus with both traces and logs makes `summary`'s
  and `errors`'s header signal field read `traces, logs`; a traces-only corpus
  makes `errors`'s read `traces` alone. (FR-29)
- **EC-24 — `sql` timestamp-literal offset is silently discarded.** Given a log
  event-time known to be `2026-07-01 10:00:00` UTC, `sql "SELECT * FROM
  logs WHERE time_unix_nano = '2026-07-01 10:00:00'"` and `sql "SELECT * FROM logs
  WHERE time_unix_nano = '2026-07-01T10:00:00Z'"` both match the row; `sql "SELECT *
  FROM logs WHERE time_unix_nano = '2026-07-01T12:00:00+02:00'"` — the same instant,
  correctly converted — does **not** match, because the offset is discarded
  rather than applied. (FR-30)
- **EC-25 — Explicit-UTC timestamp rendering.** A presented `timestamp` (or
  `summary`'s `earliest`/`latest`, or the FR-29 header's `Rows time range`) matches
  `YYYY-MM-DDTHH:MM:SS\.\d{3}Z` (exactly 3 fractional digits) in every
  `--format` — never a bare `YYYY-MM-DD HH:MM:SS` with no trailing `Z`/offset,
  and never 6-digit microseconds. (FR-16)
- **EC-26 — `compact` is the default with no `--format`.** `otelq summary` (no
  `--format` given) prints the same `{"columns":[...],"rows":[[...]]}` rendering
  as `otelq --format compact summary`; `otelq --format table summary` remains
  available as the explicit human-reading opt-in. (FR-10)
- **EC-27 — Header format line names `compact`'s shape.** `otelq --format
  compact logs` prints a header format line reading `otelq logs response,
  format compact, a {"columns":[...],"rows":[[...]]} object — column names
  once, each row a positional array`; `otelq --format json logs` prints
  `otelq logs response, format json` with no such suffix. (FR-29)
- **EC-28 — Live schema introspection reveals more than the cheat-sheet.**
  `otelq sql "DESCRIBE traces"` returns more columns than `--help`'s `sql
  views` cheat-sheet documents for `traces` — including a `span_attributes`
  column — demonstrating the FR-31 escape hatch actually works. (FR-31)
- **EC-29 — `--regex` filters and reports.** `otelq --regex ERROR logs` returns
  only rows where `ERROR` matches some cell, and the header gains `Regex
  filter applied: ERROR` and `Rows removed by regex: <N>` lines; `otelq logs`
  (no `--regex`) shows neither line. (FR-32)
- **EC-30 — Malformed `--regex` pattern.** `otelq --regex "(" logs` exits
  non-zero with a message naming the underlying `re` error, not a raw
  traceback. (FR-32, FR-17)
- **EC-31 — `--regex` rejected outside its supported commands.** `otelq
  --regex ERROR sql "SELECT 1"` (and likewise `doctor`/`collector-config`/
  `troubleshoot`) exits non-zero naming the command as unsupported for
  `--regex`, rather than silently ignoring the flag. (FR-32, FR-17)
- **EC-32 — `summary` emits a labeled service second block.** `otelq --format
  compact summary` prints the per-signal object, then a
  `** List of services in telemetry data **` delimiter line, then a second
  compact object `{"columns":["service","count"],"rows":[...]}` ordered by
  count descending; the same two-block structure appears in every `--format`
  (the second block rendered in that format), and the first block is
  byte-identical to what `summary` emitted before the second block existed.
  (FR-3, FR-10)
- **EC-33 — Stored template no longer runs.** A `triage` auto-run candidate
  whose stored invocation no longer parses (flag renamed since it was
  recorded) or fails at runtime (stale SQL against a changed schema) is
  **suggested or reported** instead of crashing triage: the parse failure
  downgrades the candidate to the printed suggestion, the runtime failure is
  named on stderr, and triage exits `0` either way. (FR-35, FR-17)
- **EC-34 — Torn or foreign history store.** An unreadable/corrupt
  `.otelq-history/` parquet yields an **empty** `history` report and routes
  `triage` to its honest-refusal path — never a traceback, and never any
  effect on the telemetry query commands themselves. (FR-34, FR-35, FR-17)

## Acceptance Criteria

> Given/When/Then, each independently testable. Command-level criteria exercise
> the in-memory `synth_conn` fixture and the direct `cmd_*` / `format_output` /
> `main` entry points; file-level robustness criteria use the file-based
> `temp_telemetry` fixture. Hints reference `just otelq`, `just otelq-test`, and
> `tests/test_otelq.py`.

- **AC-1** (Verifies FR-1, FR-2): Given a synthetic connection with traces, logs,
  and gauge/sum/histogram/exp_histogram metrics, when each relation is queried via
  `sql`, then `traces`, `logs`, `metrics`, `metrics_gauge`, `metrics_sum`,
  `metrics_histogram`, and `metrics_exp_histogram` all resolve and expose the
  columns listed in FR-2; `metrics` returns all four `metric_type` values; and the
  unified `value` equals the `sum` for the `histogram`/`exp_histogram` rows.
  *Verification hint: `cmd_sql` with `SELECT * FROM <relation> LIMIT 1` per
  relation; assert column names, the `metric_type` set, and the value-or-sum rule.*
- **AC-2** (Verifies FR-3): Given a corpus with traces of differing duration,
  logs across several levels, and metrics, when `summary` runs, then the columns
  are `signal, details, count, earliest, latest, services` in that order; traces
  yield a `>1s` and a `=<1s` row; logs yield one row for each of
  `TRACE/DEBUG/INFO/WARN/ERROR/FATAL`; metrics yield one row for each of
  `gauge/sum/histogram/exp_histogram`; and each row's
  count/earliest/latest/services are scoped to that row's subset.
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
  event-time with the presented metric output columns.
  *Verification hint: `test_metric_returns_time_series` for `db.pool.in_use`.*
- **AC-8** (Verifies FR-9): Given a synthetic connection, when `sql "SELECT
  count(*) AS n FROM traces"` runs, then the query's own columns and rows are
  returned verbatim.
  *Verification hint: `test_sql_passthrough`.*
- **AC-9** (Verifies FR-9, FR-17, FR-37, INV-5, EC-4): Given a malformed SQL
  string, when `sql` runs, then otelq exits **`2`** (not `1`) with an
  `otelq: query_error:` message on stderr, empty stdout, and no Python traceback.
  *Verification hint: invoke `cmd_sql`/`main` with `"SELEKT 1"`; assert
  `SystemExit` / exit `2` and the `otelq: query_error:` prefix.*
- **AC-10** (Verifies FR-10, INV-2): Given any result, when `--format json` is
  selected, then the output is a JSON array of objects keyed by the result
  columns; `--format csv` emits a header row plus CSV rows; `--format table` is
  the human-facing layout, opted into explicitly (not the default).
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
- **AC-15** (Verifies FR-16, EC-7): Given the real fixture, when any relation is
  queried, then the presented `timestamp` falls in the correct (near-present)
  year, not a far-future one.
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
  more than 2048 records, when otelq runs, then every record of that batch is
  returned, no size warning is printed, and the run exits `0`.
  *Verification hint: `test_ac15_robust_tail_parsing` (large-batch arm);
  assert full row count and empty stderr for the batch.*
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
  `summary` runs, then it returns exactly the four metric-type rows
  (`gauge/sum/histogram/exp_histogram`, types with no captured rows at count `0`)
  and emits no zero-count log/trace skeleton; an absent signal contributes no
  rows.
  *Verification hint: `test_summary_absent_signal_has_no_rows`.*
- **AC-26** (Verifies FR-22, FR-39, EC-13): Given the parser, when `main(["help"])`
  runs, then it prints the top-level help (its `usage:` line and the
  global-flags-first rule) to **stdout** and exits `0`; when `main(["help", "slow"])`
  runs, it prints `slow`'s own help (its `--top` flag) to stdout and exits `0`;
  `main(["help", "not-a-command"])` exits `2` with an invalid-choice message; and
  `main([])` prints the same top-level help to **stderr** and exits **`2`**
  (FR-39).
  *Verification hint: `test_bare_otelq_is_usage_error`,
  `test_help_command_prints_general_help`,
  `test_help_command_topic_prints_subcommand_help`,
  `test_help_command_unknown_topic_errors`.*
- **AC-27** (Verifies FR-23, EC-14): Given more matching log rows than the bound,
  when `logs --top 2` runs, then exactly two rows are returned and a truncation
  notice is written to stderr; when the bound is not exceeded (`--top 50`), the
  full result is returned with no stderr notice.
  *Verification hint: `test_f1_top_caps_rows_and_warns_on_stderr`; also
  `test_bug2_slow_top_negative_rejected_at_parse` for the non-negative rule.*
- **AC-28** (Verifies FR-24, EC-16): Given the CLI, when `otelq --version` runs,
  then it prints `otelq <version>` and exits `0`, and `<version>` equals the
  `pyproject.toml` project version.
  *Verification hint: `test_f3_version_flag_prints_and_exits_zero`,
  `test_f3_version_matches_pyproject`.*
- **AC-29** (Verifies FR-25, EC-17, INV-3): Given any command, when `--verbose` is
  supplied, then the stdout rows and order match the non-verbose run and a
  window/route/cache line is written to stderr.
  *Verification hint: run a command with and without `--verbose`; assert stdout is
  identical and stderr carries the plan summary.*
- **AC-30** (Verifies FR-26): Given a valid telemetry dir, when `doctor` runs,
  then it reports cache-writability (and, when a far-future cursor watermark is
  present, a clock-skew `WARN`) as non-fatal rows without changing the exit code.
  *Verification hint: `test_d4_doctor_reports_cache_health`,
  `test_d4_and_b1_doctor_flags_clock_skew`.*
- **AC-31** (Verifies FR-27, EC-20): Given a built-in command, when it runs, then
  DuckDB external file access is revoked after its relations are built (a
  `read_csv` is denied); given `sql`, file access is retained (`read_csv`
  succeeds).
  *Verification hint: `test_d2_sql_boundary_locks_builtins_not_sql`.*
- **AC-32** (Verifies FR-10, EC-18, INV-3): Given any result, when `--format json`
  is selected it is a single compact JSON array, and `--format jsonl` emits one
  compact JSON object per line; both carry the same rows in the same order as
  `table`.
  *Verification hint: `test_p5_format_json_compact_and_jsonl`,
  `test_p5_format_jsonl_via_cli`.*
- **AC-33** (Verifies FR-15, EC-15): Given `--since 30s`, when a command runs,
  then the window is the trailing 30 seconds; `_parse_since` accepts the `s` unit
  alongside `m/h/d` and rejects a malformed value.
  *Verification hint: `test_f2_since_accepts_seconds_unit`,
  `test_f2_since_seconds_windows_end_to_end`.*
- **AC-34** (Verifies FR-6, EC-19): Given several traces, when `trace` is given a
  unique id prefix it returns that trace; an exact id also works; an ambiguous
  prefix exits non-zero with an ambiguity message.
  *Verification hint: `test_f4_trace_prefix_resolves_and_flags_ambiguity`.*
- **AC-35** (Verifies FR-16, EC-7): Given a corpus with one far-future record plus
  in-window records, when a windowed command runs, then the far-future record is
  excluded (it sits beyond the window's plausibility ceiling) while the in-window
  records are returned, identically cached vs `--no-cache`; `--all` includes both.
  The record is excluded by the **ceiling**, not by a clamp on a data-derived
  anchor — since [ADR-015](../adr/ADR-015-wall-clock-query-window.md) no record
  can move the window at all.
  *Verification hint: `test_far_future_record_is_excluded_by_the_ceiling`.*
- **AC-36** (Verifies FR-28, EC-21): Given a telemetry directory whose path
  contains a single quote, when a command runs, then it returns the correct result
  with no SQL error, identical cached vs `--no-cache`.
  *Verification hint: cross-checked by the incremental-cache SPEC's path-escaping
  criteria; assert a quoted `--dir` yields the same rows on both paths.*
- **AC-37** (Verifies FR-10, EC-22, INV-2, INV-3): Given any result, when
  `--format compact` is selected it is a single compact JSON object with a
  `columns` array and a `rows` array of positional arrays; zipping each row with
  `columns` yields exactly the objects `--format json` produces, in the same
  order.
  *Verification hint: `test_p5_format_compact_columns_rows`,
  `test_p5_format_compact_via_cli`.*
- **AC-38** (Verifies FR-29, EC-23): Given any of `summary`/`errors`/`slow`/`trace`/
  `logs`/`metric`, when the command runs with any `--format`, then stdout begins
  with the fixed header (a `==========` line; `otelq <command> response, format
  <format>`; `OpenTelemetry signal: <signal>`; `OTEL source dir: <directory>`;
  `OTEL source resolved by: <mechanism>`; `Rows time range: <from> - <to>`;
  `Query window: <window>`; the UTC
  notice; the session id; a `----------` line) naming the invoked command,
  resolved format, absolute telemetry directory and the rule that chose it,
  followed by the FR-10 rendering of the result; the header's
  `<from>`/`<to>` equal the min/max `timestamp` among the returned rows.
  *Verification hint: run each of the six commands with `--format table` and
  `--format json`; assert the header's exact content lines and that the
  payload following it is unchanged from the pre-header rendering.*
- **AC-39** (Verifies FR-29, INV-6): Given `sql`, `doctor`, `collector-config`, or
  `troubleshoot`, when the command runs, then stdout contains no response header.
  *Verification hint: run each via `main`; assert stdout does not start with
  `==========`.*
- **AC-40** (Verifies FR-29): Given a command whose result has zero rows (e.g.
  `metric` with an unknown name), when it runs, then the header is still printed
  with `Rows time range: n/a - n/a` — and a populated `Query window:` (FR-46) —
  rather than failing or omitting the header.
  *Verification hint: `metric <unknown-name>`; assert the header's time-range
  line reads `n/a - n/a`.*
- **AC-41** (Verifies FR-29): Given a corpus with both traces and logs, when
  `summary` or `errors` runs, then the header's signal field reads
  `traces, logs` (both present, comma-joined in the fixed order
  `traces, logs, metrics`); given a traces-only corpus, `errors`'s header signal
  field reads `traces` alone.
  *Verification hint: vary the fixture's captured signals; assert the header's
  `OpenTelemetry signal:` line for `summary` and `errors`.*
- **AC-42** (Verifies FR-29): Given a corpus with no error-status spans and no
  error/fatal logs, when `errors` runs, then its result has zero rows and the
  header's signal field — not just the time range — reads `n/a`, since
  `errors`'s signal (unlike `slow`/`trace`/`logs`/`metric`'s fixed mapping) is
  derived from the returned rows.
  *Verification hint: a fixture with only healthy (non-error) traces/logs; run
  `errors`; assert `OpenTelemetry signal: n/a` and `Rows time range: n/a - n/a`.*
- **AC-43** (Verifies FR-30, EC-24): Given a record with a known UTC
  event-time, when `sql` filters on that value as a bare literal or as a
  `Z`-suffixed ISO-8601 literal, then both match the record; when it filters on
  the same instant written with an explicit non-`Z` offset, then it does
  **not** match, pinning that the offset is silently discarded rather than
  converted.
  *Verification hint: `test_ac43_sql_timestamp_literal_utc_convention` against
  otelq's own `traces`/`logs`/`metrics` relations (not an isolated DuckDB
  sanity check), so a DuckDB upgrade that changes this parsing behavior is
  caught.*
- **AC-44** (Verifies FR-30): Given `otelq --help`, when its text is rendered,
  then a "timestamps" block stating the UTC convention appears before the
  "argument order" section, not only inside the `sql views` cheat-sheet.
  *Verification hint:
  `test_help_epilog_documents_argument_order_and_sql_schema`; assert the
  "timestamps:" block's index precedes "argument order:"'s.*
- **AC-45** (Verifies FR-16, EC-25): Given any command that returns rows with a
  `timestamp` (or `summary`'s `earliest`/`latest`), when it is rendered in
  **each** of `table`, `json`, `jsonl`, `csv`, and `compact`, then every such
  value in every one of those five renderings matches
  `YYYY-MM-DDTHH:MM:SS\.\d{3}Z` (exactly 3 fractional digits); the FR-29
  response header's `Rows time range` values do too.
  *Verification hint: `test_ac45_timestamps_render_explicit_utc`; regex-match
  the rendered value in each format plus the header's `Rows time range` line.*
- **AC-46** (Verifies FR-10, EC-26): Given a command with no `--format` flag,
  when it runs, then its stdout equals the same command run with an explicit
  `--format compact`; `--format table` still renders the human table.
  *Verification hint: `test_ac46_compact_is_the_default_format`; compare stdout
  with and without an explicit `--format compact`.*
- **AC-47** (Verifies FR-29, EC-27): Given `--format compact`, when a header is
  printed, then its format line reads `otelq <command> response, format
  compact, a {"columns":[...],"rows":[[...]]} object — column names once, each
  row a positional array`; given any other `--format`, the format line carries
  no such suffix.
  *Verification hint: `test_ac47_compact_header_names_its_shape`; assert the
  suffix's presence for `compact` and absence for `json`/`jsonl`/`csv`/`table`.*
- **AC-48** (Verifies FR-31, EC-28): Given `otelq --help`, when its text is
  rendered, then the `sql views` section documents that its column list is a
  curated subset and names `DESCRIBE`/`PRAGMA table_info` as the way to
  explore the full live schema; given `sql "DESCRIBE traces"`, when it runs,
  then it returns more columns than the cheat-sheet lists for `traces`,
  including `span_attributes`.
  *Verification hint: `test_ac48_sql_schema_discovery_documented_and_works`;
  assert the help text mentions `DESCRIBE`/`PRAGMA`, and that a live
  `DESCRIBE traces` result's column set is a strict superset of the
  documented one and contains `span_attributes`.*
- **AC-49** (Verifies FR-32, EC-29): Given a corpus where the pattern matches
  some but not all rows of a command's result, when `--regex <pattern>` runs,
  then only the matching rows are returned, the header gains `Regex filter
  applied: <pattern>` and `Rows removed by regex: <N>` lines (`<N>` equal to
  the non-matching row count), and the payload is otherwise rendered exactly
  as FR-10 specifies; given no `--regex`, neither header line appears.
  *Verification hint: `test_ac49_regex_filters_rows_and_reports_in_header`.*
- **AC-50** (Verifies FR-32): Given a pattern that matches a value in one cell
  of a row but not others, when `--regex` runs, then the row is kept (matching
  *any* cell is sufficient) — not just a designated "message" column.
  *Verification hint: `test_ac50_regex_matches_any_cell`.*
- **AC-51** (Verifies FR-32, EC-30): Given a malformed pattern (e.g. `(`), when
  `--regex` runs, then otelq exits non-zero with a message naming the
  underlying `re` error, not a Python traceback.
  *Verification hint: `test_ac51_malformed_regex_is_a_real_error`.*
- **AC-52** (Verifies FR-32, EC-31): Given `--regex` with `sql`, `doctor`,
  `collector-config`, or `troubleshoot`, when the command runs, then otelq
  exits non-zero naming the command as unsupported for `--regex`.
  *Verification hint: `test_ac52_regex_rejected_outside_supported_commands`.*
- **AC-53** (Verifies FR-32): Given a `body` value containing a literal
  double-quote (a character `--format json` would escape), when a pattern
  matching only the unescaped form runs, then the row is still kept —
  proving the match happens against the raw cell value before FR-10
  rendering, not against the already-escaped/quoted rendered text.
  *Verification hint: `test_ac53_regex_matches_pre_render_raw_value`.*
- **AC-54** (Verifies FR-32): Given a row containing an uppercase value, when
  a lowercase pattern runs with no `(?i)` flag, then the row is excluded
  (case-sensitive by default); given the same pattern with a leading `(?i)`,
  the row is kept. Given a row with a `None` cell (e.g. a root span's
  `parent_span_id`), when a pattern matching the literal text `None` runs,
  then that row is **not** kept solely because of the `None` cell — `None`
  values are excluded from matching, not stringified into `"None"`.
  *Verification hint: `test_ac54_regex_case_sensitive_and_skips_none_cells`.*
- **AC-55** (Verifies FR-32, FR-23): Given a corpus where a matching row exists
  only beyond the `--top` cap's ordinal position, when `--regex` and a small
  `--top` both apply, then the result is empty (or omits that row) — the
  filter operates on the already-capped result, not a wider scan; raising
  `--top` recovers the match.
  *Verification hint: `test_ac55_regex_operates_on_already_capped_result`.*
- **AC-56** (Verifies FR-4, FR-6): Given an error span and a trace-correlated
  error log, when `errors` runs, then every row carries a `trace_id` column,
  and feeding the span row's `trace_id` to `trace` returns that span's tree —
  the triage→localization pivot needs no intermediate `sql` lookup.
  *Verification hint: `test_ac56_errors_rows_carry_trace_id_for_pivot`.*
- **AC-57** (Verifies FR-3, EC-32): Given a corpus with several services of
  differing total volume, when `summary` runs in any `--format`, then stdout
  carries two blocks — the unchanged per-signal breakdown, then a
  `** List of services in telemetry data **` delimiter and a `service`/`count`
  block listing each distinct service with its total record count across all
  signals, ordered by count descending; the second block is rendered in the
  selected format and the first block is byte-identical to summary's output
  without the second block.
  *Verification hint: `test_ac57_summary_service_list_second_block`.*
- **AC-58** (Verifies FR-33): Given `--session-id rca-42` on a header-bearing
  command, when it runs, then the response header's `Session:` line reads the
  supplied id verbatim, and the stderr session footer names `--session-id rca-42`.
  *Verification hint: `test_ac58_supplied_session_id_echoed_verbatim`.*
- **AC-59** (Verifies FR-33): Given no `--session-id`, when a header-bearing
  command runs, then the header's `Session:` id is a valid UUIDv7 (version 7),
  and the stderr footer advertises that same id.
  *Verification hint: `test_ac59_default_session_id_is_uuid7`.*
- **AC-60** (Verifies FR-33, INV-6): Given any command — including the
  header-less `sql`/`doctor`/`collector-config`/`troubleshoot` — when it runs,
  then the session footer is printed to **stderr** (never stdout, so `sql`'s
  stdout stays pure machine-parseable data) and no header appears on the
  header-less commands' stdout.
  *Verification hint: `test_ac60_footer_on_every_command_including_headerless`.*
- **AC-61** (Verifies FR-33): Given two consecutive invocations with no
  `--session-id`, when each runs, then they are stamped with **different**
  generated ids, so unrelated one-off calls are not conflated.
  *Verification hint: `test_ac61_generated_session_ids_are_unique_per_invocation`.*
- **AC-62** (Verifies FR-34): Given a template used 6 times 10 days ago and a
  template used twice within the last hour (both always terminal-successful)
  and a 1-day half-life, when `history` runs, then the recent template ranks
  **first** despite the lower raw use count — decayed frequency beats lifetime
  volume.
  *Verification hint: `test_history_scoring_prefers_recent_over_stale_heavy_use`.*
- **AC-63** (Verifies FR-34): Given a telemetry dir with no history store,
  when `history` runs, then a friendly notice goes to stderr, stdout carries
  no table, and the exit code is `0`.
  *Verification hint: `test_history_command_fresh_store_friendly`.*
- **AC-64** (Verifies FR-36): Given one invocation with
  `--session-id sess-explicit` and one without, when both are recorded, then
  the stored `session_id` values are exactly `sess-explicit` and NULL — a
  generated id is never stored.
  *Verification hint: `test_history_stores_only_supplied_session_ids`.*
- **AC-65** (Verifies FR-36): Given two rows 3 hours apart sharing one
  explicit session id and a third row 1 minute later under a different id,
  when sessionised, then the first two form **one** session and the third a
  **separate** one — elapsed time plays no part in either direction.
  *Verification hint: `test_sessionization_explicit_ids_bridge_and_split`.*
- **AC-70** (Verifies FR-36): Given two explicit sessions whose rows
  interleave seconds apart (concurrent agents) plus one id-less row between
  them, when `history` computes terminal rates, then each session's own last
  query scores terminal for **its** session only, and the id-less row scores
  terminal for nothing.
  *Verification hint:
  `test_sessionization_survives_interleaved_concurrent_agents`.*
- **AC-66** (Verifies FR-35): Given past sessions `summary → errors → logs`
  observed twice and an anchor session ending at `summary`, when
  `triage --session-id <anchor>` runs with thresholds met, then the concrete
  successor (`errors …`) is auto-run under the **anchor's** session id, the
  run is recorded into history under that id, and the **last stdout line** is
  the full suggested follow-up (`… --session-id <anchor> logs …`).
  *Verification hint: `test_triage_markov_autoruns_and_suggests_next`.*
- **AC-67** (Verifies FR-35, EC-33): Given a confident successor whose template
  contains a `?` placeholder (a normalised SQL literal), when triage runs,
  then **no** auto-run happens and the suggestion line carrying the stored raw
  form (and the session id) is printed instead.
  *Verification hint: `test_triage_nonconcrete_candidate_suggests_instead_of_running`.*
- **AC-68** (Verifies FR-35, EC-34): Given no history store, when `triage` runs, then
  a friendly stderr notice appears and stdout is empty (exit `0`); given a
  store whose evidence clears no threshold AND an anchor session that already
  ran `summary`, then the honest-refusal notice appears on stderr and stdout
  carries the FR-34 ranked list — no second grounding run.
  *Verification hints: `test_triage_no_history_is_friendly`,
  `test_triage_refuses_and_dumps_once_session_is_grounded`.*
- **AC-69** (Verifies FR-35): Given a store whose evidence clears no threshold
  and a session with **no** prior `summary`, when `triage` runs, then it
  auto-runs `summary` (banner naming the grounding, followed by summary's real
  response), records that run into history, and does **not** print the
  refusal notice.
  *Verification hint: `test_triage_grounds_with_summary_when_session_unseeded`.*
- **AC-71** (Verifies FR-38, FR-37, FR-17, INV-1, INV-7): Given a `--dir` naming
  a path that does **not** exist, when any store-reading command runs as a
  subprocess, then the process exits `2`, stdout is empty, stderr carries a line
  `otelq: store_not_found: …` naming the resolved absolute path, and the path
  **still does not exist** afterwards — otelq created neither the root nor any
  `.otelq-*` subtree.
  *Verification hint: `test_ac71_missing_store_dir_exits_2_and_creates_nothing`;
  assert `not path.exists()` after the run.*
- **AC-72** (Verifies FR-38, FR-18, FR-17): Given a telemetry directory that
  **exists** but holds no captured data, when any command runs, then the friendly
  no-telemetry message still appears on stderr and the process still exits `0` —
  the FR-18 path is unchanged by FR-38, which governs only an *absent* root.
  *Verification hint: `test_ac72_existing_empty_store_stays_friendly_exit_0`.*
- **AC-73** (Verifies FR-17, FR-37, INV-7): Given a malformed SQL string, when
  `sql` runs as a subprocess, then the process exits `2` — never `1` — stdout is
  empty, and stderr carries a line beginning `otelq: query_error:`.
  *Verification hint: `test_ac73_malformed_sql_exits_2_with_reason`; assert the
  exit code is exactly `2` so a regression to `1` fails.*
- **AC-74** (Verifies FR-37): Given any failing invocation under a machine
  `--format` (`json`, `jsonl`, `compact`), when it runs, then stderr additionally
  carries a single-line JSON object parseable by `json.loads`, whose `ok` is
  `false`, whose `reason` equals the token on the human line, and which carries
  `otelq_version` and `store.dir`; under `--format table` and `--format csv` no
  such object is printed. In neither case does anything appear on stdout.
  *Verification hint: `test_ac74_machine_format_emits_json_failure_object`,
  `test_ac74_human_format_omits_json_failure_object`.*
- **AC-75** (Verifies FR-39, FR-22, FR-17, INV-7): Given a bare `otelq` with no
  arguments, when it runs as a subprocess, then stdout is **empty**, the
  top-level help appears on stderr, and the process exits `2`; given
  `otelq help`, `otelq help slow`, and `otelq --help`, then the help appears on
  **stdout** and each exits `0`.
  *Verification hint: `test_ac75_bare_invocation_is_usage_error_on_stderr`,
  `test_ac75_explicit_help_stays_on_stdout_exit_0`.*
- **AC-76** (Verifies FR-17, FR-37): Given the set of invocations that fail to
  parse — an unknown subcommand, an unknown global flag, a global flag placed
  after the subcommand (FR-11), a malformed `--since` (FR-15), and an unknown
  `help` topic (FR-22) — when each runs, then every one exits `2` with empty
  stdout and a stderr line beginning `otelq: usage_error:`.
  *Verification hint: `test_ac76_parse_failures_are_usage_error_exit_2`,
  parametrised over the five invocations.*
- **AC-77** (Verifies FR-17, FR-26, INV-5): Given the full matrix of documented
  invocations — successes, empty results, friendly-empty, explicit help, and
  every failure mode above — when each runs, then **no** invocation exits `1`:
  every failure is `2` and every success is `0`. Given instead `doctor` over a
  directory that does not exist, then it exits `1` (a negative verdict, not a
  failure to answer), reports a `FAIL` row naming the directory, and creates
  nothing.
  *Verification hints: `test_ac77_failures_never_exit_1`,
  `test_ac77_doctor_keeps_verdict_exit_1`.*
- **AC-78** (Verifies FR-40, FR-41): Given a corpus whose rows carry
  `smoke.run_id` values `abc123` and `abc123xtra`, when
  `--resource-attr smoke.run_id=abc123` runs, then exactly the `abc123` rows are
  returned — byte-identical to what
  `sql "… WHERE resource_attr(resource_attributes, 'smoke.run_id') = 'abc123'"`
  returns — and the `abc123xtra` rows are **excluded**. A strict substring never
  matches.
  *Verification hint: `test_ac78_resource_attr_is_exact_not_substring`; assert the
  flag path and the macro path return the same rows.*
- **AC-79** (Verifies FR-40): Given a dotted key (`smoke.run_id`) and a row whose
  attributes are the flat `{"smoke.run_id": "abc"}` plus a decoy row whose
  attributes are the nested `{"smoke": {"run_id": "abc"}}`, when
  `--resource-attr smoke.run_id=abc` runs, then only the **flat** row is
  returned — a dotted key is a literal attribute name, not a path traversal, and
  the caller escapes nothing.
  *Verification hint: `test_ac79_dotted_key_is_literal_not_a_path`.*
- **AC-80** (Verifies FR-40): Given rows tagged with two different attributes,
  when `--resource-attr a=1 --resource-attr b=2` is supplied, then only rows
  carrying **both** are returned (conjunctive), and reversing the flag order
  returns the identical set.
  *Verification hint: `test_ac80_repeated_flags_are_conjunctive`.*
- **AC-81** (Verifies FR-40): Given one corpus carrying traces, logs, and metrics
  all tagged with the same `smoke.run_id`, when the filter is applied to
  `summary`, `errors`, `slow`, `trace`, `logs`, and `metric` in turn, then each
  returns only that run's rows — one flag, the whole correlation spine.
  *Verification hint: `test_ac81_filter_applies_across_all_six_commands`,
  looping the six commands.*
- **AC-82** (Verifies FR-40, FR-37): Given `--resource-attr` supplied to a
  command that does not support it (`sql`, `doctor`, `collector-config`,
  `troubleshoot`), when it runs, then it exits `2` with `otelq: usage_error:`
  and, for `sql`, a message naming `resource_attr(...)` as the supported route —
  never a silent no-op.
  *Verification hint: `test_ac82_resource_attr_rejected_where_unsupported`.*
- **AC-83** (Verifies FR-40, FR-37): Given a malformed `--resource-attr`
  argument — an empty key, or `key=` with an empty value — when it runs, then it
  exits `2` with `otelq: usage_error:`; and given `--resource-attr key` with no
  `=`, then rows carrying any non-empty value for `key` are returned while rows
  where it is absent or empty are not.
  *Verification hint: `test_ac83_malformed_pair_is_usage_error`,
  `test_ac83_presence_form_matches_any_nonempty_value`.*
- **AC-84** (Verifies FR-40, FR-29): Given one or more `--resource-attr` filters,
  when a signal-bearing command runs, then the response header carries exactly
  one `Resource filter applied:` line listing every filter in the order given;
  and given none, then no such line appears.
  *Verification hint: `test_ac84_header_discloses_resource_filter`.*
- **AC-85** (Verifies FR-41): Given rows whose `resource_attributes` are
  respectively valid JSON, `NULL`, the empty string, and unparseable text, when
  `resource_attr(resource_attributes, 'k')` is evaluated over all of them, then
  it returns the value for the valid row and `NULL` for the rest **without
  raising** — one malformed row can never fail the query.
  *Verification hint: `test_ac85_resource_attr_macro_never_raises`.*
- **AC-86** (Verifies FR-41): Given the worktree key, when
  `resource_attr(resource_attributes, 'otelq.worktree.id')` is evaluated, then it
  returns exactly what worktree scoping's own extraction returns, and the
  ready-to-paste clause printed by `set_resource_attributes` is expressed with
  the macro rather than raw `json_extract_string` — one mechanism, one behavior.
  *Verification hint: `test_ac86_worktree_extraction_goes_through_the_macro`.*
- **AC-87** (Verifies FR-41): Given `otelq --help`, when its `sql` section is
  read, then it documents `resource_attr(...)` as an **otelq-defined** helper
  (not OpenTelemetry, not a DuckDB builtin) and states what it returns.
  *Verification hint: `test_ac87_help_documents_the_macro_as_otelq_specific`.*

- **AC-88** (Verifies FR-42, FR-41): Given log records carrying a log attribute
  `step` and spans carrying a span attribute `step`, when `--attr step=pair-node`
  runs against `logs` and against `slow`, then each returns only its own signal's
  matching records — the column is chosen by signal, never named by the caller.
  *Verification hint: `test_ac88_attr_selects_the_signal_appropriate_column`.*
- **AC-89** (Verifies FR-42, FR-40): Given a record whose **Resource**
  attributes contain `k=v` and a different record whose **record** attributes
  contain `k=v`, when `--attr k=v` runs, then only the record-attribute row
  matches; and when `--resource-attr k=v` runs, then only the Resource row
  matches. The two namespaces never cross.
  *Verification hint: `test_ac89_record_and_resource_attributes_never_cross`.*
- **AC-90** (Verifies FR-42, FR-29): Given both `--resource-attr` and `--attr`,
  when a signal-bearing command runs, then the header carries two distinct
  lines, `Resource filter applied:` and `Attribute filter applied:`, and both
  filters are ANDed.
  *Verification hint: `test_ac90_attr_and_resource_filters_are_disclosed_separately`.*
- **AC-91** (Verifies FR-42, FR-37): Given `--attr` supplied to an unsupported
  command, or with a malformed argument, when it runs, then it exits `2` with
  `otelq: usage_error:` — identical handling to FR-40.
  *Verification hint: `test_ac91_attr_validation_matches_resource_attr`.*

- **AC-92** (Verifies FR-43): Given a trace `A -> B -> C`, when `span_tree` is
  queried, then all three rows share one `root_id` (`A`'s span id) with depths
  `0, 1, 2`, and `is_orphan` is false throughout; and `span_edges` carries
  exactly the two edges `B->A` and `C->B`.
  *Verification hint: `test_ac92_span_tree_walks_a_healthy_chain`.*
- **AC-93** (Verifies FR-43): Given a trace whose middle span names a
  `parent_span_id` that is **not** in the store, when `span_tree` is queried,
  then the trace resolves into **two** components with different `root_id`s, and
  the span with the dangling reference has `is_orphan = true` while the genuine
  root (empty `parent_span_id`) has `is_orphan = false`.
  *Verification hint: `test_ac93_dangling_parent_splits_the_component`; this is
  the case a shared-`trace_id` test reports as a false green.*
- **AC-94** (Verifies FR-43): Given two different traces that happen to contain
  the same `parent_span_id` value, when `span_tree` is queried, then no span in
  one trace shares a `root_id` with any span in the other — component membership
  never crosses a trace boundary.
  *Verification hint: `test_ac94_components_never_cross_traces`.*
- **AC-95** (Verifies FR-43, FR-14, INV-3): Given one corpus, when `span_tree`
  and `span_edges` are queried with the cache and again with `--no-cache`, then
  both return byte-identical rows; and given a store whose `traces` relation is
  empty, then both resolve to zero rows rather than raising.
  *Verification hint: `test_ac95_span_relations_are_cache_invariant_and_empty_safe`.*
- **AC-96** (Verifies FR-44, FR-43): Given a member set and a corpus containing
  (a) one healthy connected trace, (b) the same members split across two trace
  ids, and (c) a corpus missing one member entirely, when the caller runs a
  single `sql` predicate over `span_tree`, then it distinguishes all three —
  every member present in one component, every member present but split, and a
  member never observed — without otelq exposing any verdict of its own.
  *Verification hint: `test_ac96_caller_composes_the_structural_predicate`,
  using the very query `--help` documents.*
- **AC-97** (Verifies FR-44): Given such a caller-composed predicate, when it is
  run under `await` and the satisfying telemetry arrives mid-wait, then the
  invocation exits `0`; and when it never arrives, then it exits `1` — structure
  is gated entirely through the existing exit-code contract, with no new command.
  *Verification hint: `test_ac97_structural_predicate_gates_through_await`.*
- **AC-98** (Verifies FR-43, FR-44): Given `otelq --help`, when its `sql`
  section is read, then it documents `span_tree` and `span_edges` as
  **otelq-defined** derived relations and carries the worked example that
  separates a never-observed member from members split across traces.
  *Verification hint: `test_ac98_help_documents_span_relations_and_example`.*

- **AC-99** (Verifies FR-45, FR-12): Given a store at `<cwd>/.telemetry`, when a
  command runs with **no** `--dir`, then it reads that store and the header
  reports `OTEL source resolved by: CWD or an ancestor`; and given the same
  invocation from a **subdirectory** several levels down, then it resolves to the
  same store by walking upward.
  *Verification hint: `test_ac99_resolution_walks_up_from_cwd`.*
- **AC-100** (Verifies FR-45): Given both `OTELQ_DIR` and a valid
  `<cwd>/.telemetry`, when a command runs with no `--dir`, then `OTELQ_DIR` wins
  and the mechanism is reported as `OTELQ_DIR env variable`; and given `--dir`
  as well, then `--dir` wins over both and reports `--dir flag`.
  *Verification hint: `test_ac100_resolution_precedence`.*
- **AC-101** (Verifies FR-45, FR-38): Given `OTELQ_DIR` naming a directory that
  does not exist, when a command runs, then it exits `2` with `store_not_found`
  against **that** path — a typo in a deliberately-set variable is never papered
  over by falling through to discovery, because two processes sharing the
  variable would otherwise read different stores.
  *Verification hint: `test_ac101_otelq_dir_typo_is_not_silently_ignored`.*
- **AC-102** (Verifies FR-45): Given a repository whose **main checkout** holds
  the telemetry and a **linked worktree** that holds its own *empty*
  `.telemetry/`, when a command runs from the linked worktree with no `--dir`,
  then it reads the **main checkout's** store — not the local empty one — and the
  mechanism names the worktree and default branch; and when the same command runs
  from the main checkout, then the mechanism is `CWD or an ancestor`, because
  there is no redirect to make.
  *Verification hint: `test_ac102_linked_worktree_resolves_to_main_checkout`;
  create a real temp repo plus `git worktree add`.*
- **AC-103** (Verifies FR-45, FR-38, FR-37, INV-1): Given a directory tree
  containing no `.telemetry/` anywhere above the working directory and no
  `OTELQ_DIR`, when a command runs, then it exits `2` with `store_not_found`, the
  message names **every path tried**, and no directory is created.
  *Verification hint: `test_ac103_unresolvable_store_names_paths_tried`.*
- **AC-104** (Verifies FR-45, FR-29, FR-37): Given any resolution mechanism, when
  a signal-bearing command succeeds, then the header carries both
  `OTEL source dir:` (absolute) and `OTEL source resolved by:`; and when
  resolution fails under a machine `--format`, then the JSON failure object
  carries `store.dir` and `store.resolved_by`. Two invocations can therefore be
  proven to have read the same store from output alone.
  *Verification hint: `test_ac104_resolution_is_disclosed_on_success_and_failure`.*

- **AC-105** (Verifies FR-46, FR-29): Given a windowless command with no
  `--since`, when it succeeds, then the header carries
  `Query window: <from> - <to> (30m, default)` with bounds exactly 30 minutes
  apart, and the label `Rows time range:` appears on the row-derived line while
  the bare label `Time range:` appears nowhere.
  *Verification hint: `test_ac105_default_window_is_disclosed_with_origin`.*

- **AC-106** (Verifies FR-46): Given `--since 10m`, when the command succeeds,
  then the header carries `(10m, --since)` and the disclosed bounds are exactly
  10 minutes apart.
  *Verification hint: `test_ac106_since_window_is_disclosed_with_origin`.*

- **AC-107** (Verifies FR-46, FR-6): Given `--all`, then the header carries
  `Query window: all history (--all)`; and given `trace` (unbounded by
  definition), then it carries `all history (command default)`.
  *Verification hint: `test_ac107_unbounded_windows_disclose_their_origin`.*

- **AC-108** (Verifies FR-46, FR-29): Given a command whose result is **empty**
  in the searched range, when it succeeds, then `Rows time range:` renders
  `n/a - n/a` **and** `Query window:` still carries the applied bounds — an empty
  answer is never returned without the range that produced it.
  *Verification hint: `test_ac108_empty_result_still_discloses_the_window`.*

- **AC-109** (Verifies FR-46, FR-11, INV-7): Given the same invocation run with
  and without `--no-cache`, when both succeed, then both disclose byte-identical
  `Query window:` bounds.
  *Verification hint: `test_ac109_window_disclosure_matches_across_cache_paths`.*

- **AC-110** (Verifies FR-47, FR-3): Given a store with traces and logs but no
  metrics, when `summary` runs, then its output ends with a
  `** Newest record per signal **` block carrying `signal`, `newest`, `age`
  columns with one row for `traces` and one for `logs`, and **no** row for
  `metrics`.
  *Verification hint: `test_ac110_summary_reports_newest_record_per_signal`.*

- **AC-111** (Verifies FR-47, FR-44): Given a record older than the query window,
  when `summary` runs, then the freshness block still reports that record's
  event-time and age (freshness is not window-scoped), the reported age carries
  no `STALE`/`OK` verdict or threshold, and the FR-29 header carries no
  per-signal freshness line.
  *Verification hint: `test_ac111_freshness_is_window_independent_and_unjudged`.*

- **AC-112** (Verifies FR-15, FR-46): Given a store whose newest record is two
  hours old and a windowed command with no `--since`, when it runs, then it
  returns **zero** rows — the window is the last 30 minutes of wall-clock, not
  the 30 minutes preceding the newest record — and the disclosed `Query window:`
  lower bound sits 30 minutes before wall-clock rather than 30 minutes before
  that record. (The upper bound is the plausibility ceiling of FR-15, **not**
  wall-clock itself; see AC-113.)
  *Verification hint: `test_ac112_window_floor_is_wall_clock_not_newest_record`.*

- **AC-113** (Verifies FR-15, FR-16): Given a record whose event-time is
  minutes **ahead** of wall-clock (a clock-skewed producer), when a windowed
  command runs, then that record **is** returned; and given a record beyond the
  plausibility ceiling, then it is excluded. A skewed producer is never silently
  dropped.
  *Verification hint: `test_ac113_skew_ceiling_keeps_slightly_future_records`.*

- **AC-114** (Verifies FR-18, FR-47, FR-46): Given a store whose every record is
  older than the window, when `summary` runs, then it does **not** print the
  friendly no-telemetry message: it exits `0` having printed the FR-29 header
  with the applied `Query window:`, and a `** Newest record per signal **` block
  reporting the age of the newest record — so "nothing captured" and "nothing
  captured recently" are distinguishable.
  *Verification hint: `test_ac114_out_of_window_store_reports_freshness_not_empty`.*

- **AC-115** (Verifies FR-11): Given a store whose watermark is
  **ahead** of wall-clock, when the same command runs cached and with
  `--no-cache`, then both return identical rows — retention anchored at
  `min(watermark, now)` must not evict rows that are still inside the wall-clock
  window. (Verifies INV-7 and EC-12 of
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md); this file's
  own INV-7/EC-12 are unrelated requirements.)
  *Verification hint: `test_ac115_future_watermark_does_not_evict_in_window_rows`.*

- **AC-116** (Verifies FR-15, FR-46): Given a store whose records are all far
  older than any window, when the command is run with `--all`, then every record
  is returned and the header discloses `all history (--all)` — unbounded queries
  are unaffected by the clock.
  *Verification hint: `test_ac116_all_history_is_unaffected_by_the_clock`.*




### Examples

- **Argument order (FR-11).** `just otelq --format json errors` succeeds;
  `just otelq errors --format json` fails with
  `otelq: error: unrecognized arguments: --format json`. Global flags — including
  the time-window flag `--since` — precede the subcommand:
  `just otelq --since 10m errors`; per-command flags still follow it:
  `just otelq slow --top 5`.
- **Relations for `sql` (FR-1/FR-2).**
  `just otelq-sql "SELECT service_name, count(*) FROM traces WHERE status_code = 2 GROUP BY 1"`
  groups error spans by service; `metrics` unifies the per-type relations, so with
  all four present `SELECT DISTINCT metric_type FROM metrics` returns
  `{gauge, sum, histogram, exp_histogram}`. Under expose-empty (FR-1), any
  documented relation resolves to `0` rows (not a catalog error) when its signal
  has no data but some other telemetry is present — `SELECT count(*) FROM
  metrics_histogram` and `SELECT count(*) FROM traces` both return `0` on a
  gauge/sum-only corpus.
- **Friendly emptiness (FR-18 vs FR-19).** With nothing captured, every command
  prints "no telemetry captured — is the collector running …?" to stderr and
  exits 0. With only `metrics` captured, `errors` instead prints "no traces or
  logs telemetry captured (present: metrics) …", naming the gap.
- **Help discoverability (FR-22, FR-39).** `just otelq help` prints the full
  top-level help on stdout and exits `0`; `just otelq help slow` prints `slow`'s
  own help (its `--top` flag). `just otelq help nope` exits `2` with an
  invalid-choice message naming the valid commands. A bare `just otelq` prints
  that same top-level help on **stderr** and exits `2` — help is an answer only
  when help was the question.

## Invariants

- **INV-1** — Read-only over telemetry: otelq never modifies, creates, or deletes
  the raw telemetry files it reads. Its output is a pure function of (the
  telemetry it reads, the command, and the flags).
- **INV-2** — Output-format roles are fixed: `compact` (a single object with a
  `columns` header and positional `rows` arrays) is the default, lowest-token
  machine/automation format, alongside `json` (a single compact array) and
  `jsonl` (one compact object per line); `table` is the human-facing format, an
  explicit `--format table` opt-in; `csv` is the spreadsheet/interchange format.
  Choosing a format never changes which command runs.
- **INV-3** — Format independence: the rows a command returns, and their order,
  do not depend on which `--format` is chosen; only the rendering differs.
- **INV-4** — Friendly failure: absent telemetry yields a human-readable stderr
  message and exit `0`; a reader/DuckDB stack trace is never the user-facing
  result of "nothing captured" or "this signal is missing".
- **INV-5** — Exit-code discipline: exit `0` covers every success including empty
  results, the friendly "no telemetry" path, and explicitly requested help; exit
  `1` is the verdict tier — an answer that is negative, as `doctor` returns for
  an unhealthy store — and is never used for a failure to answer; exit `2` covers
  every case in which no answer was produced. (Amended by
  [ADR-012](../adr/ADR-012-exit-codes-as-public-contract.md); see FR-17.)
- **INV-7** — stdout is the answer: whenever otelq exits `0`, stdout holds the
  answer to the question that was asked, and nothing else. Whenever otelq exits
  `2`, stdout is empty and the reason is on stderr (FR-37). No diagnostic, error
  object, or unrequested help ever appears on stdout.
- **INV-6** — Header is additive, not substitutive: the FR-29 response header is
  prepended to stdout for its six governed commands but never changes the
  columns/rows a command returns (INV-3) nor the FR-10 rendering of the payload
  that follows it. For `sql`, `collector-config`, `troubleshoot`, and `doctor`,
  stdout is unchanged from FR-10's payload rendering — no header is printed.
